"""The same bounded wire contracts are used on both sides of MCP."""
import hashlib
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceChoice = Literal['blender', 'computergraphics', 'gamedev', 'wikipedia_en', 'wikipedia_ru']
SOURCE_LABELS = {'blender':'Blender Stack Exchange', 'computergraphics':'Computer Graphics Stack Exchange',
                 'gamedev':'Game Development Stack Exchange', 'wikipedia_en':'Wikipedia (en)', 'wikipedia_ru':'Wikipedia (ru)'}


def source_host(source):
    return source.removeprefix('wikipedia_') + '.wikipedia.org' if source.startswith('wikipedia_') else source + '.stackexchange.com'


def source_limitation(source):
    kind = 'вводных текстовых фрагментов статей' if source.startswith('wikipedia_') else 'фрагментов реальных ответов'
    return f'До трёх {kind} {SOURCE_LABELS[source]}; не полный обзор.'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def run_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Некорректный идентификатор запуска.')
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SearchInput(Contract):
    question: str = Field(min_length=1, max_length=2000, description='Вопрос пользователя о компьютерной графике.')
    query: str = Field(min_length=1, max_length=200, description='Короткая поисковая тема на языке выбранного источника.')
    source: SourceChoice = 'blender'

    @field_validator('question', 'query')
    @classmethod
    def clean(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError('Введите непустой текст без управляющих символов.')
        value.encode('utf-8')
        return value

    @field_validator('query')
    @classmethod
    def single_line(cls, value):
        if '\n' in value or '\t' in value:
            raise ValueError('Поисковая тема должна быть одной строкой.')
        return value


class Source(Contract):
    title: str = Field(min_length=1, max_length=240)
    url: str = Field(max_length=2048)
    excerpt: str = Field(min_length=1, max_length=1800)
    author: str = Field(default='', max_length=120)
    date: str = Field(default='', max_length=80)

    @field_validator('url')
    @classmethod
    def source_url(cls, value):
        parts = urlsplit(value)
        hosts = {source_host(source) for source in SOURCE_LABELS}
        valid_path = (parts.path.startswith('/wiki/') and len(parts.path) > 6) if parts.netloc.endswith('.wikipedia.org') else bool(re.match(r'^/(a|questions)/[0-9]+(?:/|$)', parts.path))
        if parts.scheme != 'https' or parts.netloc not in hosts or not valid_path:
            raise ValueError('Недопустимая ссылка источника.')
        return value


class Materials(SearchInput):
    run_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    provider: Literal['stackexchange', 'wikipedia'] = 'stackexchange'
    sources: list[Source] = Field(min_length=1, max_length=3)
    limitation: str = Field(default='До трёх фрагментов реальных ответов Blender Stack Exchange; не полный обзор.', max_length=300)

    @model_validator(mode='after')
    def verify_source(self):
        expected = 'wikipedia' if self.source.startswith('wikipedia_') else 'stackexchange'
        if self.provider != expected or any(urlsplit(s.url).netloc != source_host(self.source) for s in self.sources):
            raise ValueError('Материалы не соответствуют выбранному источнику.')
        return self


class Summary(Contract):
    run_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    text: str = Field(min_length=1, max_length=9000)
    sources: list[Source] = Field(min_length=1, max_length=3)
    content: str = Field(min_length=1, max_length=16000, description='Точный текст файла: памятка и атрибуция источников.')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def verify_hash(self):
        if digest(self.content) != self.sha256:
            raise ValueError('Хеш не соответствует содержимому.')
        return self


class Saved(Contract):
    run_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    filename: str = Field(pattern=r'^[0-9a-f]{32}\.txt$')
    content: str = Field(min_length=1, max_length=16000)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bytes_written: int = Field(gt=0, le=64000)

    @model_validator(mode='after')
    def verify(self):
        if self.filename != self.run_id + '.txt' or digest(self.content) != self.sha256 or len(self.content.encode('utf-8')) != self.bytes_written:
            raise ValueError('Некорректное подтверждение записи.')
        return self
