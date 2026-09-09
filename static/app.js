const form = document.querySelector('#search-form');
const applicationForm = document.querySelector('#application-form');
const input = document.querySelector('#query');
const partTypeInput = document.querySelector('#part-type');
const vehicleInput = document.querySelector('#vehicle');
const yearInput = document.querySelector('#vehicle-year');
const engineInput = document.querySelector('#engine');
const catalogSelect = document.querySelector('#catalog-select');
const applicationModeButton = document.querySelector('#application-mode-button');
const freeModeButton = document.querySelector('#free-mode-button');
const results = document.querySelector('#results');
const statusCard = document.querySelector('#status');
const template = document.querySelector('#result-template');
const voiceButton = document.querySelector('#voice-button');
const voiceStatus = document.querySelector('#voice-status');

const SpeechRecognition =
  window.SpeechRecognition || window.webkitSpeechRecognition;

let recognition = null;
let listening = false;
let recognizedText = '';

const savedCatalog = localStorage.getItem('selectedCatalog');

if (
  savedCatalog &&
  [...catalogSelect.options].some(
    option => option.value === savedCatalog
  )
) {
  catalogSelect.value = savedCatalog;
}

catalogSelect.addEventListener('change', () => {
  localStorage.setItem(
    'selectedCatalog',
    catalogSelect.value
  );
});

function setSearchMode(mode) {
  const applicationMode = mode === 'application';

  applicationForm.hidden = !applicationMode;
  form.hidden = applicationMode;

  applicationModeButton.classList.toggle(
    'active',
    applicationMode
  );

  freeModeButton.classList.toggle(
    'active',
    !applicationMode
  );

  applicationModeButton.setAttribute(
    'aria-selected',
    String(applicationMode)
  );

  freeModeButton.setAttribute(
    'aria-selected',
    String(!applicationMode)
  );

  voiceStatus.hidden = true;

  if (applicationMode) {
    partTypeInput.focus();
  } else {
    input.focus();
  }
}

applicationModeButton.addEventListener('click', () => {
  setSearchMode('application');
});

freeModeButton.addEventListener('click', () => {
  setSearchMode('free');
});

function setVoiceState(active, message = '') {
  listening = active;

  voiceButton.classList.toggle('listening', active);
  voiceButton.setAttribute('aria-pressed', String(active));

  voiceButton.querySelector('.voice-label').textContent =
    active ? 'Ouvindo…' : 'Ditar';

  voiceStatus.hidden = !message;
  voiceStatus.textContent = message;
}

if (!SpeechRecognition) {
  voiceButton.disabled = true;
  voiceButton.title =
    'Ditado não disponível neste navegador';

  voiceButton.querySelector('.voice-label').textContent =
    'Indisponível';
} else {
  recognition = new SpeechRecognition();

  recognition.lang = 'pt-BR';
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  recognition.addEventListener('start', () => {
    recognizedText = '';

    setVoiceState(
      true,
      'Ouvindo… diga a peça, a característica ou o veículo.'
    );
  });

  recognition.addEventListener('result', event => {
    let interim = '';
    let finalText = '';

    for (
      let index = event.resultIndex;
      index < event.results.length;
      index += 1
    ) {
      const transcript =
        event.results[index][0].transcript.trim();

      if (event.results[index].isFinal) {
        finalText += `${transcript} `;
      } else {
        interim += `${transcript} `;
      }
    }

    if (finalText) {
      recognizedText += finalText;
    }

    input.value =
      `${recognizedText}${interim}`.trim();
  });

  recognition.addEventListener('error', event => {
    const messages = {
      'not-allowed':
        'Permissão do microfone negada. Libere o microfone nas configurações do navegador.',

      'audio-capture':
        'Nenhum microfone foi encontrado neste aparelho.',

      'no-speech':
        'Não ouvi nenhuma fala. Clique no microfone e tente novamente.',

      'network':
        'O reconhecimento de voz está temporariamente indisponível.'
    };

    recognizedText = '';

    setVoiceState(
      false,
      messages[event.error] ||
        'Não foi possível reconhecer a fala. Tente novamente.'
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

    if (shouldSearch) {
      form.requestSubmit();
    }
  });

  voiceButton.addEventListener('click', () => {
    if (listening) {
      recognition.stop();
      return;
    }

    try {
      recognition.start();
    } catch (error) {
      setVoiceState(
        false,
        'Aguarde um instante e tente novamente.'
      );
    }
  });
}

document.querySelectorAll('[data-query]').forEach(button => {
  button.addEventListener('click', () => {
    setSearchMode('free');

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

async function performSearch(query) {
  if (listening && recognition) {
    recognition.stop();
  }

  if (query.length < 2) {
    showMessage(
      'Digite sua pesquisa',
      'Use um código, veículo, peça, ano ou código equivalente.'
    );

    return;
  }

  results.innerHTML =
    '<div class="loading">Consultando o catálogo…</div>';

  statusCard.hidden = true;

  try {
    const response = await fetch('/api/search', {
      method: 'POST',

      headers: {
        'Content-Type': 'application/json'
      },

      body: JSON.stringify({
        query,
        catalog_id: catalogSelect.value || null
      })
    });

    const contentType =
      response.headers.get('content-type') || '';

    if (!contentType.includes('application/json')) {
      throw new Error(
        response.ok
          ? 'Resposta inválida do servidor.'
          : 'Servidor temporariamente indisponível. Tente novamente.'
      );
    }

    const data = await response.json();

    if (!response.ok) {
      throw new Error(
        data.error || 'Falha na consulta'
      );
    }

    results.innerHTML = '';

    if (!data.results.length) {
      showMessage(
        'Nenhuma aplicação confirmada',
        'Tente retirar algum detalhe ou pesquisar somente pelo código ou modelo do veículo.'
      );

      return;
    }

    if (data.answer) {
      const answerCard =
        document.createElement('article');

      answerCard.className =
        `answer-card ${
          data.answer.supported
            ? 'supported'
            : 'not-supported'
        }`;

      const label =
        document.createElement('span');

      label.className = 'answer-label';

      label.textContent =
        data.answer.supported
          ? 'RESPOSTA CONFIRMADA NO CATÁLOGO'
          : 'INFORMAÇÃO NÃO LOCALIZADA';

      const answerText =
        document.createElement('p');

      answerText.textContent = data.answer.text;

      answerCard.append(label, answerText);

      if (
        data.answer.sources &&
        data.answer.sources.length
      ) {
        const source =
          document.createElement('small');

        source.textContent =
          'Fonte: ' +
          data.answer.sources
            .map(item => {
              const pages =
                item.pages && item.pages.length
                  ? `, página${
                      item.pages.length > 1
                        ? 's'
                        : ''
                    } ${item.pages.join(', ')}`
                  : '';

              return (
                `${item.manufacturer} ` +
                `${item.edition} · ` +
                `${item.code}${pages}`
              );
            })
            .join(' | ');

        answerCard.appendChild(source);
      }

      results.appendChild(answerCard);

      if (data.answer.supported) {
        return;
      }
    }

    const heading =
      document.createElement('div');

    heading.className = 'results-title';

    const count =
      document.createElement('strong');

    count.textContent =
      `${data.results.length} resultado` +
      `${data.results.length > 1 ? 's' : ''}`;

    const context =
      document.createElement('span');

    if (
      data.ai_used &&
      data.interpreted_query &&
      data.interpreted_query.toUpperCase() !==
        query.toUpperCase()
    ) {
      context.textContent =
        `para “${query}” · ` +
        `IA interpretou: “${data.interpreted_query}”`;
    } else {
      context.textContent = `para “${query}”`;
    }

    heading.append(count, context);
    results.appendChild(heading);

    data.results.forEach(item => {
      const card =
        template.content.cloneNode(true);

      const resultCard =
        card.querySelector('.result-card');

      if (item.display_text) {
        resultCard.classList.add(
          'focused-result'
        );
      }

      card.querySelector(
        '.manufacturer'
      ).textContent = item.manufacturer;

      card.querySelector(
        '.code'
      ).textContent = item.code;

      const visibleDescription =
        item.display_text || item.text;

      card.querySelector(
        '.description'
      ).textContent = visibleDescription
        .replace(item.code, '')
        .replace(/\s*\|\s*/g, '\n')
        .trim();

      card.querySelector(
        '.category'
      ).textContent =
        item.category || 'Autopeças';

      const pages =
        Array.isArray(item.pages)
          ? item.pages
          : [];

      card.querySelector(
        '.pages'
      ).textContent =
        pages.length
          ? `Página${
              pages.length > 1 ? 's' : ''
            } ${pages.join(', ')}`
          : 'Página não informada';

      card.querySelector(
        '.edition'
      ).textContent = item.edition || '';

      card.querySelector(
        '.raw-text'
      ).textContent = item.text;

      results.appendChild(card);
    });
  } catch (error) {
    showMessage(
      'Não foi possível pesquisar',
      error.message
    );
  }
}

applicationForm.addEventListener(
  'submit',
  event => {
    event.preventDefault();

    const partType =
      partTypeInput.value.trim();

    const vehicle =
      vehicleInput.value.trim();

    const year =
      yearInput.value.trim();

    const engine =
      engineInput.value.trim();

    if (!partType || !vehicle) {
      showMessage(
        'Preencha os campos principais',
        'Informe pelo menos o tipo da peça e o veículo.'
      );

      return;
    }

    if (
      year &&
      !/^(19|20)\d{2}$/.test(year)
    ) {
      yearInput.focus();

      showMessage(
        'Ano inválido',
        'Digite o ano com quatro números, por exemplo: 2005.'
      );

      return;
    }

    const structuredQuery = [
      partType,
      vehicle,
      year,
      engine
    ]
      .filter(Boolean)
      .join(' ');

    performSearch(structuredQuery);
  }
);

form.addEventListener('submit', event => {
  event.preventDefault();

  performSearch(input.value.trim());
});
