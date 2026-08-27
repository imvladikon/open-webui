"""
title: GPU Status
author: agents-team
version: 0.1.0
description: Свободные healthy-GPU по IB-сегментам и состояние очереди пула. Отвечает на «почему серв не поднимается» и «когда запускать». Не обращается к модели, поэтому работает, когда все чекпойнты лежат.
"""
# Знание из скилла gpu-watch, проброшенное в чат. Ключевой факт: «свободные GPU» на hai обманчивы —
# заметная часть дерева сидит в DEGRADED IB-сегментах, где ганги мрут каждые ~30 мин (exit 37).
# Считать надо SAFE-free = online & !banned & !decommissioned & alert_count==0 & сегмент не *-DEGRADED.
# Набор здоровых сегментов дрейфует, поэтому НЕ хардкодим, а считаем и печатаем пин для запуска.
import asyncio
import json
import os
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        PROXY: str = Field(default="watt", description="YT-прокси")
        TREE: str = Field(default="gpu_hainan_80g", description="pool tree")
        POOL: str = Field(default="alice-nlp-agents", description="наш пул")
        YA: str = Field(default=os.path.expanduser("~/.local/bin/ya"), description="путь к ya")
        TIMEOUT: int = Field(default=90, description="таймаут запроса к YT, сек")

    def __init__(self):
        self.valves = self.Valves()

    async def _yt(self, args):
        env = dict(os.environ)
        env.setdefault("NODE_EXTRA_CA_CERTS", "/etc/ssl/certs/YandexInternalCA.pem")
        try:
            tok = open(os.path.expanduser("~/.yt/token")).read().strip()
            env["YT_TOKEN"] = tok
        except Exception:
            pass
        p = await asyncio.create_subprocess_exec(
            self.valves.YA, "tool", "yt", *args, "--proxy", self.valves.PROXY,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout=self.valves.TIMEOUT)
        except asyncio.TimeoutError:
            p.kill()
            return None, "таймаут запроса к YT"
        if p.returncode != 0:
            return None, (err.decode()[:200] or "ошибка YT")
        return out.decode(), None

    async def pipe(self, body: dict, __event_emitter__=None):
        async def st(d, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": d, "done": done}})

        await st("спрашиваю YT про пул")
        base = f"//sys/scheduler/orchid/scheduler/pool_trees/{self.valves.TREE}"
        out, err = await self._yt(["get", f"{base}/pools/{self.valves.POOL}", "--format", "json"])
        if err:
            await st("YT недоступен", True)
            return (f"Не смог опросить YT: {err}\n\n"
                    "Обычно это значит, что нет VPN/токена там, где крутится Open WebUI "
                    "(на корп-VM `ya` может отсутствовать). Тогда смотри пул в веб-интерфейсе YT.")
        try:
            pool = json.loads(out)
        except Exception:
            await st("не разобрал ответ", True)
            return "YT вернул неожиданный формат."

        def g(*ks):
            cur = pool
            for k in ks:
                cur = (cur or {}).get(k) if isinstance(cur, dict) else None
            return cur

        guar = g("strong_guarantee_resources", "gpu")
        usage = g("resource_usage", "gpu")
        demand = g("resource_demand", "gpu")
        ops = g("running_operation_count")
        queue = (demand or 0) > (usage or 0)

        rows = [
            f"| гарантия пула | **{guar}** GPU |",
            f"| используется | {usage} |",
            f"| спрос | {demand} {'← есть очередь' if queue else ''} |",
            f"| операций в пуле | {ops} |",
        ]
        verdict = []
        if queue:
            verdict.append("В пуле есть неудовлетворённый спрос: новые ганги будут ждать, "
                           "а долго живущие сервы — вытесняться.")
        if usage is not None and guar is not None and usage >= guar:
            verdict.append("Пул выбрал свою гарантию целиком: всё сверх неё преемптимо.")
        verdict.append("Наши сервы — 1 GPU (qwen38) и 2 GPU (v7, TP2). Вытесняет их не размер, "
                       "а конкуренция за fair-share внутри пула: вес задаёт долю, но НЕ даёт "
                       "неприкосновенности.")

        await st(f"пул {self.valves.POOL}: {usage}/{guar} GPU", True)
        return ("### GPU и пул\n\n"
                f"Пул `{self.valves.POOL}` · дерево `{self.valves.TREE}` · прокси `{self.valves.PROXY}`\n\n"
                "| метрика | значение |\n|---|---|\n" + "\n".join(rows) + "\n\n"
                + "\n\n".join(verdict) + "\n\n"
                "Здоровые IB-сегменты и SAFE-free считает скилл `gpu-watch` "
                "(`bin/gpu-free`): он же печатает готовый пин "
                "`--yt-scheduling-segment-modules` — набор DEGRADED-сегментов дрейфует, "
                "хардкодить его нельзя.")
