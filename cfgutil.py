"""config.yaml 的共用讀取與驗證。

snapshot.py 和 update_cash.py 都要加總 cash / debt,而 YAML 不會做運算——
寫成 `amount: 100000+50000` 會被當成字串,原本只會噴
`TypeError: unsupported operand type(s) for +: 'int' and 'str'`,
看不出是哪一區、哪一筆、該怎麼改。這裡先驗證再加總。
"""


class ConfigError(Exception):
    pass


def sum_amounts(items, section: str) -> float:
    """加總 config 的 cash / debt 區塊,金額不是數字就報清楚的錯。"""
    total = 0.0
    for item in items or []:
        name = item.get("name", "(未命名)")
        amount = item.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ConfigError(
                f"config.yaml 的 {section} 區塊「{name}」金額不是數字:{amount!r}\n"
                f"  YAML 不會做運算,不能寫成 100000+50000 這種算式。\n"
                f"  多筆請拆成多個項目,程式會自動加總:\n"
                f"    {section}:\n"
                f"      - name: 第一筆\n"
                f"        amount: 100000\n"
                f"      - name: 第二筆\n"
                f"        amount: 50000"
            )
        total += float(amount)
    return total
