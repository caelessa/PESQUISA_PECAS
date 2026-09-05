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
    const heading = document.createElement('div');
    heading.className = 'results-title';
    heading.innerHTML = `<strong>${data.results.length} resultado${data.results.length > 1 ? 's' : ''}</strong><span>para “${query}”</span>`;
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

