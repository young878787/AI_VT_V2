import os
from dotenv import load_dotenv

# 載入 .env 檔案中的設定
load_dotenv()

try:
    from google.cloud import texttospeech_v1beta1 as texttospeech
except ImportError:
    print("無法匯入 Google TTS SDK。請確定已安裝套件: pip install google-cloud-texttospeech")
    exit(1)

def test_google_tts():
    print("正在測試 Google Cloud TTS API (使用 ADC)...")
    try:
        # SDK 會自動讀取使用 gcloud auth application-default login 定義的身分憑證
        client = texttospeech.TextToSpeechClient()
        
        # 從 .env 中讀取語言參數，若無可用預設值 (對應當前設定)
        language = os.getenv("TTS_LANGUAGE", "cmn-CN")
        voice_name = os.getenv("TTS_VOICE_NAME", "cmn-CN-Chirp3-HD-Aoede")
        
        print(f"使用的語言: {language}")
        print(f"使用的語音: {voice_name}")
        
        text = "你好，這是一段測試語音，這代表 Google TTS API 以及登入身分驗證運作正常！"
        print(f"正在合成語音文字: {text}")
        
        # 1. 設定文字內容
        synthesis_input = texttospeech.SynthesisInput(text=text)
        
        # 2. 設定語音選項
        voice = texttospeech.VoiceSelectionParams(
            language_code=language,
            name=voice_name,
        )
        
        # 3. 設定要匯出的音訊格式
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
        )
        
        # 4. 發送請求合成
        response = client.synthesize_speech(
            input=synthesis_input, 
            voice=voice, 
            audio_config=audio_config
        )
        
        # 5. 保存音訊
        output_file = "test_tts_output.mp3"
        with open(output_file, "wb") as out:
            out.write(response.audio_content)
            
        print(f"🎉 測試成功！音訊檔案已經儲存為: '{output_file}'")
            
    except Exception as e:
        print(f"❌ 測試失敗！請檢查是否已執行 `gcloud auth application-default login` 或專案是否開通了權限：\n詳細錯誤訊息: {e}")

if __name__ == "__main__":
    test_google_tts()
