def scan(query: str):
    query = query or ""
    patterns = [
        ("' OR 1=1", "Possible tautology/injection pattern."),
        ("--", "SQL comment marker can be abused when SQL is built unsafely."),
        ("DROP", "Destructive DDL keyword detected."),
        ("UNION SELECT", "UNION-based injection pattern detected."),
        ("' OR 'a'='a", "Possible tautology/injection pattern."),
    ]

    risks = []
    for pattern, explanation in patterns:
        if pattern.lower() in query.lower():
            risks.append({"pattern": pattern, "message": explanation})

    return risks
