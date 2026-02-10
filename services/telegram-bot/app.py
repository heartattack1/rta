from flask import Flask, jsonify
import os

app = Flask(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "service")


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
