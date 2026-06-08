import os

def get_api_key():
    from dotenv import load_dotenv
    load_dotenv()
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise ValueError("No GROQ_API_KEY found in .env file")
    return key