const TBL = document.querySelector('#profiles tbody');
const FORM = document.querySelector('#create-form');

async function api(path, method='GET', body) {
  const res = await fetch(path, {
    method,
    headers: body ? {'Content-Type':'application/json'} : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function statusBadge(state) {
  const span = document.createElement('span');
  span.className = `status ${state}`;
  span.textContent = state;
  return span;
}

function drawRows(rows) {
  TBL.innerHTML = '';
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.dataset.id = r.id;

    const tdName = document.createElement('td');
    tdName.textContent = r.name;

    const tdState = document.createElement('td');
    const sb = statusBadge(r.state || 'stopped');
    tdState.appendChild(sb);

    const tdUrl = document.createElement('td');
    tdUrl.textContent = r.last_url || '';

    const tdActions = document.createElement('td');
    tdActions.className = 'actions';
    const startBtn = document.createElement('button');
    startBtn.textContent = 'Start';
    startBtn.onclick = async () => {
      await api(`/api/profiles/${r.id}/start`, 'POST');
      await refresh();
    };
    const stopBtn = document.createElement('button');
    stopBtn.textContent = 'Stop';
    stopBtn.onclick = async () => {
      await api(`/api/profiles/${r.id}/stop`, 'POST');
      await refresh();
    };
    const delBtn = document.createElement('button');
    delBtn.textContent = 'Delete';
    delBtn.onclick = async () => {
      await api(`/api/profiles/${r.id}`, 'DELETE');
      await refresh();
    };

    tdActions.append(startBtn, stopBtn, delBtn);

    tr.append(tdName, tdState, tdUrl, tdActions);
    TBL.appendChild(tr);
  }
}

async function refresh() {
  const rows = await api('/api/profiles');
  drawRows(rows);
}

FORM.addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.querySelector('#name').value.trim();
  const proxy = document.querySelector('#proxy').value.trim();
  if (!name) return;
  await api('/api/profiles', 'POST', {name, proxy: proxy || null});
  FORM.reset();
  await refresh();
});

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'state') {
        const row = document.querySelector(`tr[data-id="${msg.profile_id}"]`);
        if (row) {
          const stateTd = row.children[1];
          stateTd.innerHTML = '';
          stateTd.appendChild(statusBadge(msg.state));
          row.children[2].textContent = msg.url || '';
        }
      }
    } catch (e) {}
  };
  ws.onclose = () => setTimeout(connectWS, 1000);
}
connectWS();
refresh();
