"""
AI Tool 定義：提供給 LLM 的 function calling 工具清單。
純資料結構，無任何外部相依。
"""

tools: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_ai_behavior",
            "description": "設定 Live2D 模型的即時表情與動作參數。每次回覆都必須呼叫，用來搭配當下的心情。參數請善用小數點，像調色盤一樣自由創作表情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "head_intensity": {
                        "type": "number",
                        "description": "身體活動幅度 0.0（靜止）到 1.0（非常激動）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "blush_level": {
                        "type": "number",
                        "description": "臉紅程度 0.0（無）到 1.0（極度害羞）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "eye_sync": {
                        "type": "boolean",
                        "description": "是否同步雙眼（與眉毛）。False 可做出眨單眼、不對稱表情。",
                    },
                    "eye_l_open": {
                        "type": "number",
                        "description": "左眼張開程度 0.0（閉眼）到 1.0（全開）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "eye_r_open": {
                        "type": "number",
                        "description": "右眼張開程度 0.0（閉眼）到 1.0（全開）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "duration_sec": {
                        "type": "number",
                        "description": "動作持續時間（秒），通常 3.0 到 15.0",
                        "minimum": 2.0,
                        "maximum": 20.0,
                    },
                    "mouth_form": {
                        "type": "number",
                        "description": "嘴角形狀。-1.0=悲傷委屈下垂，0.0=自然，+1.0=開心上揚大笑",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_l_y": {
                        "type": "number",
                        "description": "左眉毛高低位置。-1.0=眉頭下壓，+1.0=左眉上揚",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_r_y": {
                        "type": "number",
                        "description": "右眉毛高低位置。-1.0=眉頭下壓，+1.0=右眉上揚",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_l_angle": {
                        "type": "number",
                        "description": "左眉毛角度。-1.0=八字眉，0.0=水平，+1.0=倒八字眉",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_r_angle": {
                        "type": "number",
                        "description": "右眉毛角度。-1.0=八字眉，0.0=水平，+1.0=倒八字眉",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_l_form": {
                        "type": "number",
                        "description": "左眉毛彎曲。-1.0=下彎，0.0=自然，+1.0=上凸",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_r_form": {
                        "type": "number",
                        "description": "右眉毛彎曲。-1.0=下彎，0.0=自然，+1.0=上凸",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "speaking_rate": {
                        "type": "number",
                        "description": "語音語速。1.0=正常。開心興奮時加快(1.1-1.4)，傷心沉思時放慢(0.7-0.9)，撒嬌時稍慢(0.95)。",
                        "minimum": 0.5,
                        "maximum": 1.8,
                    },
                },
                "required": [
                    "head_intensity",
                    "blush_level",
                    "eye_sync",
                    "eye_l_open",
                    "eye_r_open",
                    "duration_sec",
                    "mouth_form",
                    "brow_l_y",
                    "brow_r_y",
                    "brow_l_angle",
                    "brow_r_angle",
                    "brow_l_form",
                    "brow_r_form",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_profile",
            "description": "更新主人的畫像。當偵測到主人提到新的喜好、性格、興趣、生日、重要決定等個人特徵時呼叫。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "update"],
                        "description": "操作類型：add（新增）、remove（移除）、update（更新）",
                    },
                    "field": {
                        "type": "string",
                        "enum": [
                            "core_traits",
                            "dislikes",
                            "recent_interests",
                            "communication_style",
                            "custom_notes",
                        ],
                        "description": "要更新的欄位",
                    },
                    "value": {
                        "type": "string",
                        "description": "要新增/移除/更新的內容",
                    },
                },
                "required": ["action", "field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory_note",
            "description": "記錄重要事件。當對話中發生值得長期記住的事件時呼叫（例如一起討論了有趣話題、主人分享了經歷）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要記錄的事件內容",
                    }
                },
                "required": ["content"],
            },
        },
    },
]
