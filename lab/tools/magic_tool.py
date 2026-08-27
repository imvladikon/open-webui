"""
title: Magic Number
author: agents-team
"""
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        SECRET: str = Field(default="4242", description="секретное число сервера")

    def __init__(self):
        self.valves = self.Valves()

    def get_magic_number(self, __event_emitter__=None) -> str:
        """
        Вернуть секретное магическое число этого сервера. Вызывать, когда спрашивают магическое число.
        """
        return f"The magic number is {self.valves.SECRET}."
