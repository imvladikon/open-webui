"""
title: Plot Data
author: agents-team
version: 0.1.0
description: График из чисел прямо в чате: CSV или JSON превращается в компактную vega-lite спеку (bar/line/scatter), которую Open WebUI рендерит нативно. Для сравнения чекпойнтов, метрик по шагам, распределений.
"""
# Почему vega-lite, а не картинка: OWUI рендерит блок ```vega-lite инлайн (CodeBlock.svelte),
# спека компактная (~1 КБ) и модель её осилит вывести дословно — в отличие от 17 КБ HTML,
# на которых мы уже обжигались. Данные парсим САМИ, модель их не переписывает.
import csv
import io
import json
import re

_NUM = re.compile(r"^-?\d+(?:[.,]\d+)?$")
# палитра из нашей дизайн-системы (references/style-guide.md скилла diagram-design)
_ACCENT = "#eb6c36"
_INK = "#2d3142"


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    return float(s.replace(",", ".")) if _NUM.match(s) else None


def _parse(data: str):
    """CSV или JSON -> список словарей. Разделитель угадываем (таб/;/,)."""
    s = (data or "").strip()
    if not s:
        return None, "пустые данные"
    if s.startswith("[") or s.startswith("{"):
        try:
            obj = json.loads(s)
        except Exception as e:
            return None, f"JSON не разобрался: {e}"
        rows = obj if isinstance(obj, list) else [obj]
        return [r for r in rows if isinstance(r, dict)], None
    head = s.splitlines()[0]
    delim = "\t" if "\t" in head else (";" if head.count(";") > head.count(",") else ",")
    try:
        rows = list(csv.DictReader(io.StringIO(s), delimiter=delim))
    except Exception as e:
        return None, f"CSV не разобрался: {e}"
    return rows, None


class Tools:
    def __init__(self):
        pass

    def plot_data(self, data: str, chart: str = "bar", x: str = "", y: str = "",
                  title: str = "") -> str:
        """
        Построить график по данным пользователя (CSV или JSON-массив объектов). Использовать, когда есть числа и надо увидеть их картинкой: сравнение моделей, метрика по шагам, распределение.
        :param data: данные в CSV (с заголовком) или JSON-массиве объектов
        :param chart: тип графика: bar, line, scatter или area
        :param x: имя колонки для оси X (если пусто, берётся первая)
        :param y: имя колонки для оси Y (если пусто, берётся первая числовая)
        :param title: заголовок графика
        """
        rows, err = _parse(data)
        if err:
            return f"Ошибка: {err}"
        if not rows:
            return "Ошибка: не нашёл ни одной строки данных."

        cols = list(rows[0].keys())
        if not cols:
            return "Ошибка: в данных нет колонок."
        xf = x or cols[0]
        if xf not in cols:
            return f"Ошибка: колонки {x!r} нет. Есть: {', '.join(cols)}"
        if y:
            yf = y
            if yf not in cols:
                return f"Ошибка: колонки {y!r} нет. Есть: {', '.join(cols)}"
        else:
            yf = next((c for c in cols if c != xf and any(_num(r.get(c)) is not None for r in rows)),
                      None)
            if not yf:
                return f"Ошибка: не нашёл числовую колонку. Есть: {', '.join(cols)}"

        values, skipped = [], 0
        for r in rows:
            yv = _num(r.get(yf))
            if yv is None:
                skipped += 1
                continue
            values.append({xf: r.get(xf), yf: yv})
        if not values:
            return f"Ошибка: в колонке {yf!r} нет чисел."

        ctype = {"bar": "bar", "line": "line", "scatter": "point", "area": "area"}.get(chart, "bar")
        x_type = "quantitative" if all(_num(v[xf]) is not None for v in values) else "nominal"
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": title or f"{yf} по {xf}",
            "width": 520, "height": 260,
            "data": {"values": values},
            "mark": {"type": ctype, "tooltip": True,
                     **({"point": True} if ctype == "line" else {}),
                     "color": _ACCENT if ctype in ("bar", "area") else _INK},
            "encoding": {
                "x": {"field": xf, "type": x_type,
                      "sort": None if x_type == "quantitative" else "-y",
                      "axis": {"labelAngle": 0 if x_type == "quantitative" else -30}},
                "y": {"field": yf, "type": "quantitative"},
                "tooltip": [{"field": xf}, {"field": yf}],
            },
        }
        note = f"\n\n_{len(values)} точек" + (f", пропущено {skipped} нечисловых" if skipped else "") + "_"
        return ("ГОТОВЫЙ ГРАФИК. Выведи блок ДОСЛОВНО (он отрисуется), после него максимум одна "
                "строка комментария.\n\n```vega-lite\n"
                + json.dumps(spec, ensure_ascii=False) + "\n```" + note)
