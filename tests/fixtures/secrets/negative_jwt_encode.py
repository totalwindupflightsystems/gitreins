token = jwt.encode(payload, secret_key, algorithm="HS256")
decoded = jwt.decode(token, secret_key, algorithms=["HS256"])
encoded = b64encode(data)
