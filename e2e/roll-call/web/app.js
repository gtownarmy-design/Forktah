'use strict';

function placeholder(agent) {
  const div = document.createElement('div');
  div.className = 'avatar placeholder';
  div.setAttribute('role', 'img');
  div.setAttribute('aria-label', `${agent.name} (no avatar)`);
  div.textContent = (agent.name || agent.id || '?').trim().charAt(0).toUpperCase() || '?';
  return div;
}

function card(agent) {
  const li = document.createElement('li');
  li.className = 'card';

  const frame = document.createElement('div');
  frame.className = 'frame';
  const img = document.createElement('img');
  img.className = 'avatar';
  img.alt = `${agent.name} avatar`;
  img.width = 96;
  img.height = 96;
  img.addEventListener('error', () => img.replaceWith(placeholder(agent)), { once: true });
  img.src = agent.avatar;
  frame.append(img);

  const name = document.createElement('h2');
  name.className = 'name';
  name.textContent = agent.name;

  const role = document.createElement('p');
  role.className = 'role';
  role.textContent = agent.role;

  const provider = document.createElement('p');
  provider.className = 'provider';
  provider.textContent = agent.provider;

  li.append(frame, name, role, provider);
  return li;
}

async function main() {
  const status = document.getElementById('status');
  const list = document.getElementById('agents');
  try {
    const res = await fetch('/api/agents');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const agents = await res.json();
    list.replaceChildren(...agents.map(card));
    status.textContent = `${agents.length} agents`;
  } catch (err) {
    status.textContent = `Could not load agents: ${err.message}`;
    status.classList.add('error');
  }
}

main();
