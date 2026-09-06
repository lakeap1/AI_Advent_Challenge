const form = document.querySelector('#form');
const promptField = document.querySelector('#prompt');
const submit = document.querySelector('#submit');
const example = document.querySelector('#example');
const status = document.querySelector('#status');
const columns = [...document.querySelectorAll('.result')];

example.addEventListener('click', () => { promptField.value = promptField.dataset.example; promptField.focus(); });
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled || !promptField.value.trim()) return;
  submit.disabled = example.disabled = promptField.disabled = true;
  status.textContent = 'Отправлены три запроса. Ждём ответы…';
  for (const column of columns) {
    column.className = 'result loading';
    column.querySelector('.state').textContent = 'Генерация…';
    column.querySelector('.answer').textContent = '';
    column.querySelector('.error').hidden = true;
  }
  try {
    const response = await fetch('/api/compare', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt:promptField.value})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить сравнение.');
    for (const [i, result] of data.results.entries()) {
      const column = columns[i];
      column.className = `result ${result.status}`;
      column.querySelector('.state').textContent = `${result.status === 'completed' ? 'Готово' : 'Не завершено'} · ${result.seconds} с${result.usage ? ` · ${result.usage.output_tokens} токенов ответа` : ''}`;
      column.querySelector('.answer').textContent = result.answer || '';
      if (result.error) { const error = column.querySelector('.error'); error.textContent = result.error; error.hidden = false; }
    }
    const completed = data.results.filter(r => r.status === 'completed').length;
    status.textContent = `${completed} из 3 ответов готовы.${data.save_warning ? ' ' + data.save_warning : ''}`;
  } catch (error) {
    status.textContent = error.message || 'Ошибка сети. Проверьте соединение.';
    for (const column of columns) { column.className = 'result'; column.querySelector('.state').textContent = 'Ответ не получен'; }
  } finally {
    submit.disabled = example.disabled = promptField.disabled = false;
  }
});
