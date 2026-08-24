const candidates = {
  chenguang: {
    name: '澄光微纳（虚构）',
    event: '完成首批客户样件验证',
    quote: '“本句为纯合成演示引文：首批工程样件已完成约定测试流程，后续仍需客户书面验收。”',
    unknown: '测试指标、客户身份与后续订单尚未独立确认',
    next: '取得测试报告与客户书面确认',
  },
  yunlan: {
    name: '云岚精密（虚构）',
    event: '交付首套精密检测样机',
    quote: '“本句为纯合成演示引文：首套检测样机已送达测试现场，但尚未形成正式验收结论。”',
    unknown: '验收标准、交付金额与回款安排尚未确认',
    next: '核对验收单、合同及回款计划',
  },
};

function switchTab(name) {
  document.querySelectorAll('[data-tab]').forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('[data-panel]').forEach((panel) => {
    const active = panel.dataset.panel === name;
    panel.hidden = !active;
    panel.classList.toggle('active-panel', active);
  });
}

function selectCandidate(id) {
  const item = candidates[id];
  if (!item) return;
  document.getElementById('evidence-name').textContent = item.name;
  document.getElementById('evidence-event').textContent = item.event;
  document.getElementById('evidence-quote').textContent = item.quote;
  document.getElementById('evidence-unknown').textContent = item.unknown;
  document.getElementById('evidence-next').textContent = item.next;
  document.querySelectorAll('[data-candidate]').forEach((button) => {
    button.classList.toggle('active', button.dataset.candidate === id);
  });
}

document.querySelectorAll('[data-tab]').forEach((button) => {
  button.addEventListener('click', () => switchTab(button.dataset.tab));
});

document.querySelectorAll('[data-open-evidence]').forEach((button) => {
  button.addEventListener('click', () => {
    selectCandidate(button.dataset.openEvidence);
    switchTab('evidence');
    document.getElementById('demo').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

document.querySelectorAll('[data-candidate]').forEach((button) => {
  button.addEventListener('click', () => selectCandidate(button.dataset.candidate));
});

document.querySelectorAll('[data-filter]').forEach((button) => {
  button.addEventListener('click', () => {
    const filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((item) => item.classList.toggle('active', item === button));
    document.querySelectorAll('[data-source-status]').forEach((row) => {
      row.hidden = filter !== 'all' && row.dataset.sourceStatus !== filter;
    });
  });
});

document.getElementById('start-demo').addEventListener('click', () => {
  switchTab('radar');
  document.getElementById('demo').scrollIntoView({ behavior: 'smooth', block: 'start' });
});

document.getElementById('refresh-demo').addEventListener('click', (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = '模拟刷新中…';
  window.setTimeout(() => {
    button.disabled = false;
    button.textContent = '已是最新';
    window.setTimeout(() => { button.textContent = '模拟刷新'; }, 1200);
  }, 700);
});
