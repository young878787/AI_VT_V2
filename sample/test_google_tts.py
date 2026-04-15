# -*- coding: utf-8 -*-
"""
Google Cloud TTS Chirp 3 HD API 測試腳本

使用前請確保：
1. 已安裝 google-cloud-texttospeech: pip install google-cloud-texttospeech
2. 已設定 GOOGLE_APPLICATION_CREDENTIALS 環境變數指向服務帳戶金鑰
3. 已在 Google Cloud Console 啟用 Text-to-Speech API

測試步驟：
1. 在 Google Cloud Console 建立專案
2. 啟用 Cloud Text-to-Speech API
3. 建立服務帳戶並下載 JSON 金鑰
4. 設定環境變數或修改下方的 credentials_path
5. 執行此腳本
"""

import os
import sys
from pathlib import Path

# 將 backend 加入 path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


def test_tts_basic():
    """基本 TTS 測試"""
    print("=" * 60)
    print("Google Cloud TTS Chirp 3 HD 測試")
    print("=" * 60)

    # 檢查環境變數
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials_path:
        print("\n❌ 錯誤: GOOGLE_APPLICATION_CREDENTIALS 環境變數未設定")
        print("\n請執行以下步驟：")
        print("1. 前往 Google Cloud Console (https://console.cloud.google.com/)")
        print("2. 建立專案並啟用 Cloud Text-to-Speech API")
        print("3. 建立服務帳戶並下載 JSON 金鑰")
        print("4. 設定環境變數：")
        print("   Windows: set GOOGLE_APPLICATION_CREDENTIALS=path/to/key.json")
        print("   Linux/Mac: export GOOGLE_APPLICATION_CREDENTIALS=path/to/key.json")
        return False

    if not os.path.exists(credentials_path):
        print(f"\n❌ 錯誤: 找不到金鑰檔案: {credentials_path}")
        return False

    print(f"\n✓ 金鑰檔案: {credentials_path}")

    # 嘗試載入 SDK
    try:
        from google.cloud import texttospeech_v1beta1 as texttospeech

        print("✓ google-cloud-texttospeech SDK 載入成功")
    except ImportError:
        print("\n❌ 錯誤: google-cloud-texttospeech 未安裝")
        print("請執行: pip install google-cloud-texttospeech")
        return False

    # 建立客戶端
    try:
        client = texttospeech.TextToSpeechClient()
        print("✓ TTS 客戶端初始化成功")
    except Exception as e:
        print(f"\n❌ 客戶端初始化失敗: {e}")
        return False

    # 列出可用的 Chirp 3 HD 語音
    print("\n--- 可用的 Chirp 3 HD 語音 ---")
    try:
        voices_response = client.list_voices()
        chirp3_voices = [v for v in voices_response.voices if "Chirp3-HD" in v.name]

        if chirp3_voices:
            # 按語言分組
            by_language = {}
            for v in chirp3_voices:
                lang = v.language_codes[0] if v.language_codes else "unknown"
                if lang not in by_language:
                    by_language[lang] = []
                by_language[lang].append(v.name)

            for lang, voices in sorted(by_language.items()):
                print(f"\n{lang}:")
                for voice in voices[:3]:  # 只顯示前 3 個
                    print(f"  - {voice}")
                if len(voices) > 3:
                    print(f"  ... 還有 {len(voices) - 3} 個")
        else:
            print("未找到 Chirp 3 HD 語音")

    except Exception as e:
        print(f"列出語音失敗: {e}")

    # 測試合成
    print("\n--- 語音合成測試 ---")

    test_cases = [
        ("ja-JP", "ja-JP-Chirp3-HD-Aoede", "こんにちは！今日はいい天気ですね！"),
        ("cmn-CN", "cmn-CN-Chirp3-HD-Achernar", "哇！你好呀！今天过得怎么样？"),
        ("en-US", "en-US-Chirp3-HD-Aoede", "Hello! How are you doing today?"),
    ]

    output_dir = Path(__file__).parent / "tts_output"
    output_dir.mkdir(exist_ok=True)

    for lang, voice_name, text in test_cases:
        print(f"\n測試 {lang} ({voice_name})...")
        print(f"  文字: {text}")

        try:
            voice = texttospeech.VoiceSelectionParams(
                language_code=lang,
                name=voice_name,
            )

            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=1.0,
            )

            synthesis_input = texttospeech.SynthesisInput(text=text)

            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )

            # 儲存音訊
            output_path = output_dir / f"test_{lang.replace('-', '_')}.mp3"
            with open(output_path, "wb") as f:
                f.write(response.audio_content)

            print(f"  ✓ 成功！音訊已儲存到: {output_path}")
            print(f"    大小: {len(response.audio_content)} bytes")

        except Exception as e:
            print(f"  ✗ 失敗: {e}")

    print("\n" + "=" * 60)
    print("測試完成！")
    print(f"音訊檔案已儲存到: {output_dir}")
    print("=" * 60)

    return True


def test_speaking_rate():
    """測試不同語速"""
    print("\n--- 語速測試 ---")

    try:
        from google.cloud import texttospeech_v1beta1 as texttospeech

        client = texttospeech.TextToSpeechClient()
    except Exception as e:
        print(f"初始化失敗: {e}")
        return

    output_dir = Path(__file__).parent / "tts_output"
    output_dir.mkdir(exist_ok=True)

    text = "哇！這也太有趣了吧！讓我想想..."
    rates = [0.7, 1.0, 1.3]

    for rate in rates:
        print(f"\n語速 {rate}x...")

        try:
            voice = texttospeech.VoiceSelectionParams(
                language_code="cmn-CN",
                name="cmn-CN-Chirp3-HD-Achernar",
            )

            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=rate,
            )

            synthesis_input = texttospeech.SynthesisInput(text=text)

            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )

            output_path = output_dir / f"test_rate_{str(rate).replace('.', '_')}.mp3"
            with open(output_path, "wb") as f:
                f.write(response.audio_content)

            print(f"  ✓ 成功！儲存到: {output_path}")

        except Exception as e:
            print(f"  ✗ 失敗: {e}")


if __name__ == "__main__":
    if test_tts_basic():
        test_speaking_rate()
