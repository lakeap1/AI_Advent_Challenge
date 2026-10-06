"""Ephemeral, current-message evidence for ordinary question coverage."""


def validate_question_parts(proposal, prompt):
    if type(proposal) is not dict or set(proposal) != {
            'revision', 'operations', 'question_parts'}:
        raise ValueError('Неверные поля подготовки диалога.')
    rows = proposal['question_parts']
    if type(rows) is not list or not 1 <= len(rows) <= 3:
        raise ValueError('Нужно от одной до трёх частей вопроса.')
    parts = []
    for ordinal, row in enumerate(rows, 1):
        if type(row) is not dict or set(row) != {'evidence'}:
            raise ValueError('Неверные поля части вопроса.')
        evidence = row['evidence']
        if (type(evidence) is not str or not evidence.strip()
                or len(evidence) > 1000 or evidence not in prompt):
            raise ValueError('Часть вопроса не подтверждена текущим сообщением или превышает лимит.')
        try:
            evidence.encode('utf-8')
        except UnicodeEncodeError as error:
            raise ValueError('Текст части вопроса содержит некорректный Unicode.') from error
        parts.append({'id': f'q{ordinal}', 'question': evidence, 'evidence': evidence})
    if sum(len(part['evidence']) for part in parts) > 3000:
        raise ValueError('Части вопроса превышают общий лимит.')
    return parts
