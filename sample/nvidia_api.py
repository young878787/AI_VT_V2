from openai import OpenAI
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

client = OpenAI(
  base_url = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
  api_key = os.getenv("NVIDIA_API_KEY")
)

if not client.api_key:
    raise ValueError("NVIDIA_API_KEY is not set in .env file")

completion = client.chat.completions.create(
  model="qwen/qwen3-next-80b-a3b-instruct",
  messages=[{"role":"user","content":"早安"}],
  temperature=0.2,
  top_p=0.7,
  max_tokens=8192,
  extra_body={"chat_template_kwargs": {"thinking":True}},
  stream=True
)

for chunk in completion:
  reasoning = getattr(chunk.choices[0].delta, "reasoning_content", None)
  if reasoning:
    print(reasoning, end="")
  if chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")
  

