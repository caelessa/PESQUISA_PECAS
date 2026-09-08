const form = document.querySelector('#search-form');
const input = document.querySelector('#query');
const results = document.querySelector('#results');
const statusCard = document.querySelector('#status');
const template = document.querySelector('#result-template');
const voiceButton = document.querySelector('#voice-button');
const voiceStatus = document.querySelector('#voice-status');

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let listening = false;
let recognizedText = '';

function setVoiceState(active, message = '') {
  listening = active;
  voiceButton.classList.toggle('listening', active);
  voiceButton.setAttribute('aria-pressed', String(active));
  voiceButton.querySelector('.voice-label').textContent = active ? 'Ouvindo…' : 'Ditar';
  voiceStatus.hidden = !message;
  voiceStatus.textContent = message;
}

if (!SpeechRecognition) {
  voiceButton.disabled = true;
  voiceButton.title = 'Ditado não disponível neste navegador';
  voiceButton.querySelector('.voice-label').textContent = 'Indisponível';
} else {
  recognition = new SpeechRecognition();
  recognition.lang = 'pt-BR';
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  recognition.addEventListener('start', () => {
    recognizedText = '';
    setVoiceState(true, 'Ouvindo… diga a peça, a característica ou o veículo.');
  });

  recognition.addEventListener('result', event => {
    let interim = '';
    let finalText = '';

    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const transcript = event.results[index][0].transcript.trim();

      if (event.results[index].isFinal) {
        finalText += `${transcript} `;
      } else {
        interim += `${transcript} `;
      }
    }

    if (finalText) recognizedText += finalText;
    input.value = `${recognizedText}${interim}`.trim();
  });

  recognition.addEventListener('error', event => {
    const messages = {
      'not-allowed': 'Permissão do microfone negada. Libere o microfone nas configurações do navegador.',
      'audio-capture': 'Nenhum microfone foi encontrado neste aparelho.',
      'no-speech': 'Não ouvi nenhuma fala. Clique no microfone e tente novamente.',
      'network': 'O reconhecimento de voz está temporariamente indisponível.',
    };

    recognizedText = '';
    setVoiceState(
      false,
      messages[event.error] || 'Não foi possível reconhecer a fala. Tente novamente.'
    );
  });

  recognition.addEventListener('end', () => {
    const shouldSearch =
      listening &&
      input.value.trim().length >= 2 &&
      recognizedText.trim();

    setVoiceState(
      false,
      shouldSearch
        ? 'Fala reconhecida. Pesquisando…'
        : voiceStatus.textContent
    );

    if (shouldSearch) form.requestSubmit();
  });

  voiceButton.addEventListener('click', () => {
    if (listening) {
      recognition.stop();
      return;
    }

    try {
      recognition.start();
    } catch (error) {
      setVoiceState(false, 'Aguarde um instante e tente novamente.');
    }
  });
}

document.querySelectorAll('[data-query]').forEach(button => {
  button.addEventListener('click', () => {
    input.value = button.dataset.query;
    form.requestSubmit();
  });
});

function showMessage(title, message) {
  results.innerHTML = `
    <div class="empty">
      <strong>${title}</strong>
      <p>${message}</p>
    </div>
  `;
}

form.addEventListener('submit', async event => {
  event.preventDefault();

  const query = input.value.trim();

  if (listening && recognition) recognition.stop();

  if (query.length < 2) {
    return showMessage(
      'Digite sua pesquisa',
      'Use um código, veículo, peça, ano ou código equivalente.'
    );
  }

  results.innerHTML = '<div class="loading">Consultando o catálogo…</div>';
  statusCard.hidden = true;

  try {
    const response = await fetch('/api/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query})
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || 'Falha na consulta');
    }

    results.innerHTML = '';

    if (!data.results.length) {
      return showMessage(
        'Nenhuma aplicação confirmada',
        'Tente retirar algum detalhe ou pesquisar somente pelo código ou modelo do veículo.'
      );
    }

    if (data.answer) {
      const answerCard = document.createElement('article');
      answerCard.className =
        `answer-card ${data.answer.supported ? 'supported' : 'not-supported'}`;

      const label = document.createElement('span');
      label.className = 'answer-label';
      label.textContent = data.answer.supported
        ? 'RESPOSTA CONFIRMADA NO CATÁLOGO'
        : 'INFORMAÇÃO NÃO LOCALIZADA';

      const answerText = document.createElement('p');
      answerText.textContent = data.answer.text;

      answerCard.append(label, answerText);

      if (data.answer.sources && data.answer.sources.length) {
        const source = document.createElement('small');

        source.textContent =
          'Fonte: ' +
          data.answer.sources.map(item => {
            const pages =
              item.pages && item.pages.length
                ? `, página${item.pages.length > 1 ? 's' : ''} ${item.pages.join(', ')}`
                : '';

            return `${item.manufacturer} ${item.edition} · ${item.code}${pages}`;
          }).join(' | ');

        answerCard.appendChild(source);
      }

      results.appendChild(answerCard);

      // Quando a pergunta técnica já foi respondida diretamente,
      // não mostra novamente toda a aplicação abaixo.
      if (data.answer.supported) return;
    }

    const heading = document.createElement('div');
    heading.className = 'results-title';

    const count = document.createElement('strong');
    count.textContent =
      `${data.results.length} resultado${data.results.length > 1 ? 's' : ''}`;

    const context = document.createElement('span');

    if (
      data.ai_used &&
      data.interpreted_query &&
      data.interpreted_query.toUpperCase() !== query.toUpperCase()
    ) {
      context.textContent =
        `para “${query}” · IA interpretou: “${data.interpreted_query}”`;
    } else {
      context.textContent = `para “${query}”`;
    }

    heading.append(count, context);
    results.appendChild(heading);

    data.results.forEach(item => {
      const card = template.content.cloneNode(true);

      if (item.display_text) {
        card.querySelector('.result-card').classList.add('focused-result');
      }

      card.querySelector('.manufacturer').textContent = item.manufacturer;
      card.querySelector('.code').textContent = item.code;

      // Exibe somente as aplicações relacionadas ao veículo pesquisado.
      // O texto completo permanece disponível em detalhes.
      const visibleDescription = item.display_text || item.text;

      card.querySelector('.description').textContent =
        visibleDescription.replace(item.code, '').trim();

      card.querySelector('.category').textContent =
        item.category || 'Autopeças';

      card.querySelector('.pages').textContent =
        `Página${item.pages.length > 1 ? 's' : ''} ${item.pages.join(', ')}`;

      card.querySelector('.edition').textContent = item.edition;

      // Mantém a aplicação completa dentro da área expansível.
      card.querySelector('.raw-text').textContent = item.text;

      results.appendChild(card);
    });
  } catch (error) {
    showMessage('Não foi possível pesquisar', error.message);
  }
});
