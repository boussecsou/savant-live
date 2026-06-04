import asyncio
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

MODEL = "models/gemini-3.1-flash-live-preview"
PROMPT = "Say hello and confirm you are SAVANT, a voice agent."


async def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set — check your .env file")

    client = genai.Client(api_key=api_key)

    # gemini-3.1-flash-live-preview is audio-only; enable output transcription
    # so we can print the spoken response as text
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    print(f"Connecting to Gemini Live ({MODEL})…")
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=PROMPT)]),
            turn_complete=True,
        )

        print("Response:")
        audio_chunks = 0
        async for response in session.receive():
            # print transcription of the audio as it arrives
            if (
                response.server_content
                and response.server_content.output_transcription
                and response.server_content.output_transcription.text
            ):
                print(response.server_content.output_transcription.text, end="", flush=True)

            # count audio chunks for confirmation
            if response.data:
                audio_chunks += 1

            if response.server_content and response.server_content.turn_complete:
                break

    print(f"\n[{audio_chunks} audio chunk(s) received — session closed cleanly]")


if __name__ == "__main__":
    asyncio.run(main())
