"""只檢查執行、來源隔離與 DB／prompt 證據，不對回答做語意評分。"""


def check_turn(case: dict, step: dict, record: dict, previous: list[dict], state: dict) -> None:
    from uuid import UUID
    from tools.memory_testset import step_mode

    def own_source_ids(row):
        event_id = row.get('memory_event_id')
        return {str(UUID(event_id))} & set(row.get('memory_source_ids', [])) if event_id else set()
    mode = step_mode(case, step)
    traces = record.get('trace', [])
    context = next((t for t in traces if t.get('stage') == 'chat_context'), {})
    retrieval = next((t for t in traces if t.get('stage') == 'retrieval'), {})
    applied = next((t for t in traces if t.get('stage') == 'test_mode'), {})
    checks = []

    def check(layer, name, actual, expected, valid, reason):
        checks.append(dict(layer=layer, name=name, actual=actual, expected=expected,
                           passed=bool(valid), reason='' if valid else reason))

    check('execution_status', 'chat_complete',
          [record.get('stream_complete'), bool(record.get('reply')), record.get('emotion_source'), record.get('expression')],
          '完整文字、情緒及表情事件', record.get('stream_complete') and record.get('reply') and record.get('emotion_source') and record.get('expression'),
          '缺少完整回答、emotion_update 或 expression_plan')
    check('execution_status', 'jev_chat_trace', [t.get('stage') for t in traces], 'jev / chat / chat_context',
          all(any(t.get('stage') == s for t in traces) for s in ('jev', 'chat', 'chat_context')),
          '缺少 JEV／Chat 執行或裁切後 context trace')
    jev = next((t for t in traces if t.get('stage') == 'jev'), {})
    check('execution_status', 'jev_call', jev.get('error'), None, not jev.get('error'), 'JEV 呼叫失敗並使用 fallback')
    expected_mode = dict(mode=mode.value, short_term_enabled=mode.short_term,
                         memory_read_enabled=mode.memory_read, memory_write_enabled=mode.memory_write)
    actual_mode = {k: applied.get(k) for k in expected_mode}
    check('isolation_status', 'applied_mode', actual_mode, expected_mode, actual_mode == expected_mode, '後端實際路徑模式與案例不符')
    check('isolation_status', 'memory_read', bool(retrieval), mode.memory_read,
          bool(retrieval) == mode.memory_read, 'DB 召回路徑未依模式執行')
    check('isolation_status', 'memory_write', bool(record.get('memory_event_id')), mode.memory_write,
          bool(record.get('memory_event_id')) == mode.memory_write, '記憶事件建立未依模式執行')
    if not mode.memory_write:
        check('isolation_status', 'read_only_db', record.get('db_unchanged'), True,
              record.get('db_unchanged') is True and not record.get('memory_audit') and not record.get('memory_changes'),
              '唯讀回合仍修改 DB jobs／sources／evidence／audit 或正式記憶')
    if not mode.short_term:
        actual = {k: context.get(k) for k in ('history', 'summary', 'jev_recent_dialogue')}
        check('isolation_status', 'empty_short_term', actual, 'history／summary／JEV recent dialogue 全空',
              bool(context) and not any(actual.values()) and len(context.get('messages', [])) == 2,
              '長期 probe 的 history／summary／JEV recent dialogue 非空')
    if mode.memory_read:
        check('memory_evidence_status', 'retrieval_errors', retrieval.get('errors', []), [],
              not retrieval.get('errors'), 'DB／embedding 召回執行失敗')
        candidates = retrieval.get('candidates', [])
        qualified = all(c.get('exact_match') or isinstance(c.get('similarity'), (int, float)) and c['similarity'] >= .75 for c in candidates)
        check('memory_evidence_status', 'qualified_candidates',
              [{k: c.get(k) for k in ('id', 'similarity', 'exact_match')} for c in candidates],
              '詞面命中或 cosine >= 0.75', qualified, 'Chat 召回包含不合格候選')
    if case['case_type'] == 'unknown_memory' and step['phase'] == 'probe':
        check('memory_evidence_status', 'unknown_memory',
              [len(retrieval.get('candidates', [])), context.get('injected_memory_ids', [])], [0, []],
              not retrieval.get('candidates') and not context.get('injected_memory_ids'), '未知記憶仍有合格候選或 DB prompt 注入')

    items = state.get('items', {})
    evidence = state.get('evidence', [])
    sources = state.get('sources', [])
    audit = record.get('memory_audit', [])
    route, status = record.get('memory_route'), record.get('memory_job_status')
    if mode.memory_write:
        actual = [route, status, record.get('memory_route_finalized')]
        valid_pairs = {('none', 'ignored'), ('needs_context', 'buffered'), ('process', 'done'), ('process', 'ignored')}
        check('execution_status', 'terminal_job', actual, '合法 route/status 且 route_finalized',
              (route, status) in valid_pairs and record.get('memory_route_finalized'), '記憶工作未合法結案')
        if status in {'ignored', 'buffered'}:
            check('memory_evidence_status', 'no_mutation', [audit, record.get('memory_changes')], '無 audit／正式 mutation',
                  not audit and not record.get('memory_changes'), 'ignored／buffered 工作仍提交正式 mutation')
        if route == 'none':
            ids = {s['id'] for s in sources if s['id'] in {str(UUID(record['memory_event_id']))}}
            check('memory_evidence_status', 'noise_no_evidence', sorted(ids), [],
                  not ids and not any(e['source_id'] in ids for e in evidence), 'none + ignored 留下 source／evidence')
        if status == 'buffered':
            own_jobs = {j['id'] for j in state.get('jobs', []) if j.get('conversation_id') == next(
                (s['conversation_id'] for s in sources if s['id'] in record.get('memory_source_ids', [])), None)}
            check('memory_evidence_status', 'buffered_context', record.get('memory_missing_context'), '非空 missing_context／同 session context',
                  bool(record.get('memory_missing_context')) and set(record.get('memory_context_job_ids', [])) <= own_jobs,
                  '待補工作缺少 missing_context 或引用其他 session')
        if status == 'done' or step.get('memory_expectation') == 'stored':
            target_ids = {a['target_id'] for a in audit if a.get('target_id')}
            source_ids = {s['id'] for s in sources if s.get('speaker') == 'user'} & own_source_ids(record)
            linked = {e['memory_id'] for e in evidence if e['source_id'] in source_ids} & target_ids
            valid = route == 'process' and status == 'done' and any(items.get(i, {}).get('status') == 'active' for i in linked)
            check('memory_evidence_status', 'committed_user_evidence', sorted(linked), 'audit → active item → 本輪 user source/evidence',
                  valid, '必要 setup 沒有正式 audit、active item 與合法 user evidence')
        expectation = step.get('memory_expectation', 'optional')
        if expectation in {'ignored', 'buffered'}:
            check('memory_evidence_status', 'expected_job_status', status, expectation, status == expectation, '工作狀態不符合案例允許結果')

    history = '\n'.join(m.get('content', '') for m in context.get('messages', [])[1:-1])
    # 使用 summary 原始來源區間，排除裁切後已移除的摘要；不從整份 system 猜來源。
    summary = context.get('summary', '')[:4000]
    start = context.get('summary_section_start')
    summary_fragments = []
    if start is not None:
        for left, right in context.get('system_retained_ranges', []):
            lo, hi = max(left, start), min(right, start + len(summary))
            if lo < hi:
                summary_fragments.append(summary[lo - start:hi - start])
    retained_summary = '\n'.join(summary_fragments)
    context['retained_summary'] = retained_summary
    source_locations = []
    for fact in step.get('evidence', []):
        ref = next((r for r in previous if r.get('turn') == fact['source_step']), {})
        fragments = fact['fragments']
        if fact['source'] == 'db':
            ids = own_source_ids(ref)
            linked = {e['memory_id'] for e in evidence if e['source_id'] in ids}
            injected = linked & set(context.get('injected_memory_ids', []))
            valid = bool(injected)
            actual = dict(source_ids=sorted(ids), memory_ids=sorted(linked), injected_ids=sorted(injected))
            reason = '目標 user source 沒有 evidence 連結至裁切後 prompt 的 DB 記憶'
            # 綜合來源隔離：長期事實不得在實際近期 history 或 summary 重複提供。
            if case['case_type'] in {'cross_source_synthesis', 'temporary_constraint'}:
                exclusive = not all(f in history or f in retained_summary for f in fragments)
                check('isolation_status', f'db_only_step_{fact["source_step"]}', exclusive, True, exclusive,
                      '綜合案例的 DB 事實也存在於短期來源，無法證明跨來源')
        else:
            in_history = all(f in history for f in fragments)
            in_summary = all(f in retained_summary for f in fragments)
            valid = {'history': in_history, 'summary': in_summary, 'context': in_history or in_summary}[fact['source']]
            actual = dict(history=in_history, summary=in_summary, fragments=fragments)
            reason = '目標短期片段沒有留在指定的裁切後 history／summary'
            if fact['source'] == 'summary':
                # 任一目標片段仍被 assistant 重述都不能當成純 summary 證明。
                valid = valid and not any(f in history for f in fragments)
                reason = 'summary 未保留目標片段，或近期 history 仍有 user／assistant 重述目標'
            if case['case_type'] in {'cross_source_synthesis', 'temporary_constraint'}:
                projected = '\n'.join(f.get('text', '') for f in context.get('memory_fragments', []))
                exclusive = not all(f in projected for f in fragments)
                check('isolation_status', f'context_only_step_{fact["source_step"]}', exclusive, True, exclusive,
                      '綜合案例的短期事實也存在於 DB 投影')
        source_locations.append(dict(**fact, actual=actual))
        check('memory_evidence_status' if fact['source'] == 'db' else 'context_evidence_status',
              f'source_step_{fact["source_step"]}_{fact["source"]}', actual, '來源事實實際進入裁切後 prompt', valid, reason)
    for version in step.get('versions', []):
        old_record = next((r for r in previous if r.get('turn') == version['old_step']), {})
        new_record = next((r for r in previous if r.get('turn') == version['new_step']), {})
        old_ids = {a['target_id'] for a in old_record.get('memory_audit', []) if a.get('target_id')}
        old = [items[i] for i in old_ids if i in items]
        new_ids = {e['memory_id'] for e in evidence if e['source_id'] in own_source_ids(new_record)}
        new = [items[i] for i in new_ids if i in items and items[i]['status'] == 'active']
        operations = {a['action'] for a in new_record.get('memory_audit', [])}
        valid = bool(old and new) and all(i['status'] != 'active' for i in old) and bool(operations) and operations <= set(version['allowed_operations'])
        check('memory_evidence_status', 'current_history_versions',
              dict(old_ids=sorted(old_ids), old_statuses=[i['status'] for i in old], new_active_ids=sorted(new_ids), operations=sorted(operations)),
              '舊版本保留為 history／新版本 active／合法操作集合', valid,
              '更正後仍有舊 active 版本、缺少歷史或新值，或操作不在允許集合')
    record['source_evidence'] = source_locations
    record['hard_checks'] = checks
    record['hard_status'] = {layer: 'failed' if any(not c['passed'] for c in checks if c['layer'] == layer) else 'passed'
                             for layer in ('execution_status', 'isolation_status', 'memory_evidence_status', 'context_evidence_status')}


def answer_result(record: dict) -> str:
    reasons = list(record.get('errors', []))
    reasons += [c['reason'] for c in record.get('hard_checks', []) if not c['passed']]
    return '錯誤：' + '；'.join(dict.fromkeys(reasons)) if reasons else record.get('reply', '') or '錯誤：缺少完整回答'
