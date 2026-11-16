def calculate(rows, payload):
    totals={}; excluded=[]; evidence={}
    for row in rows:
        if not row.get("valid", True): excluded.append(row["factor_id"]); continue
        value=float(row["quantity"])*float(row["factor"]); totals[row["product"]]=round(totals.get(row["product"],0)+value,3); evidence.setdefault(row["product"],[]).append(row["factor_id"])
    return {"footprints":totals,"excluded_factors":excluded,"evidence":evidence}
