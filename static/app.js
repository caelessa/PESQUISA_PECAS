const form = document.querySelector('#search-form');
const input = document.querySelector('#query');
const results = document.querySelector('#results');
const statusCard = document.querySelector('#status');
const template = document.querySelector('#result-template');

document.querySelectorAll('[data-query]').forEach(button => {
  button.addEventListener('click', () => {
    input.value = button.dataset.query;
    form.requestSubmit();
  });
});

function showMessage(title, message) {
  results.innerHTML = `<div class="empty"><strong>${title}</strong><p>${message}</p></div>`;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const query = input.value.trim();
  if (query.length < 2) return showMessage('Digite sua pesquisa', 'Use um código, veículo, peça, ano ou código equivalente.');
  results.innerHTML = '<div class="loading">Consultando o catálogo…</div>';
  statusCard.hidden = true;
  try {
    const response = await fetch('/api/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Falha na consulta');
    results.innerHTML = '';
    if (!data.results.length) {
      return showMessage('Nenhuma aplicação confirmada', 'Tente retirar algum detalhe ou pesquisar somente pelo código ou modelo do veículo.');
    }
    if (data.answer) {
      const answerCard = document.createElement('article');
      answerCard.className = `answer-card ${data.answer.supported ? 'supported' : 'not-supported'}`;
      const label = document.createElement('span');
      label.className = 'answer-label';
      label.textContent = data.answer.supported ? 'RESPOSTA CONFIRMADA NO CATÁLOGO' : 'INFORMAÇÃO NÃO LOCALIZADA';
      const answerText = document.createElement('p');
      answerText.textContent = data.answer.text;
      answerCard.append(label, answerText);
      if (data.answer.sources && data.answer.sources.length) {
        const source = document.createElement('small');
        source.textContent = 'Fonte: ' + data.answer.sources.map(item => {
          const pages = item.pages && item.pages.length ? `, página${item.pages.length > 1 ? 's' : ''} ${item.pages.join(', ')}` : '';
          return `${item.manufacturer} ${item.edition} · ${item.code}${pages}`;
        }).join(' | ');
        answerCard.appendChild(source);
      }
      results.appendChild(answerCard);
    }
    const heading = document.createElement('div');
    heading.className = 'results-title';
    const count = document.createElement('strong');
    count.textContent = `${data.results.length} resultado${data.results.length > 1 ? 's' : ''}`;
    const context = document.createElement('span');
    if (data.ai_used && data.interpreted_query && data.interpreted_query.toUpperCase() !== query.toUpperCase()) {
      context.textContent = `para “${query}” · IA interpretou: “${data.interpreted_query}”`;
    } else {
      context.textContent = `para “${query}”`;
    }
    heading.append(count, context);
    results.appendChild(heading);
    data.results.forEach(item => {
      const card = template.content.cloneNode(true);
      card.querySelector('.manufacturer').textContent = item.manufacturer;
      card.querySelector('.code').textContent = item.code;
      card.querySelector('.description').textContent = item.text.replace(item.code, '').trim();
      card.querySelector('.category').textContent = item.category || 'Autopeças';
      card.querySelector('.pages').textContent = `Página${item.pages.length > 1 ? 's' : ''} ${item.pages.join(', ')}`;
      card.querySelector('.edition').textContent = item.edition;
      card.querySelector('.raw-text').textContent = item.text;
      results.appendChild(card);
    });
  } catch (error) {
    showMessage('Não foi possível pesquisar', error.message);
  }
});
