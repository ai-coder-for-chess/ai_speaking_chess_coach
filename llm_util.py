import requests, json

SYSTEM = (
    "Ты русский шахматный тренер-гроссмейстер. "
    "Всегда отвечай на русском. Пиши кратко и по делу, максимум 5 пунктов. "
    "В ответе избегай англицизмов, используй стандартные шахматные термины: "
    "профилактика, перевести ладью, вскрыть линию, упрощение, цейтнот и т.п."
)


def ask(user_prompt: str) -> str:
    payload = {
        "model": "llama3:8b",
        "prompt": f"{SYSTEM}\n\n{user_prompt}",
        "stream": False
    }
    r = requests.post("http://localhost:11434/api/generate",
                      data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    return r.json()["response"].strip()
