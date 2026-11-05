// Motion preference: "paused" is remembered, set before anything animates, and shared by every page.
(function () {
  const KEY = 'tw-paused';
  const root = document.documentElement;
  try { if (localStorage.getItem(KEY) === '1') root.classList.add('paused'); } catch (e) { /* storage may be blocked */ }
  const btn = document.getElementById('motionToggle');
  if (!btn) return;
  const label = btn.querySelector('.ml'), icon = btn.querySelector('.mi');
  function sync() {
    const paused = root.classList.contains('paused');
    btn.setAttribute('aria-pressed', String(paused));
    label.textContent = paused ? 'Play animations' : 'Pause animations';
    icon.textContent = paused ? '▶' : '❚❚';
    if (paused) document.querySelectorAll('.float').forEach(el => { el.style.setProperty('--tx', '0px'); el.style.setProperty('--ty', '0px'); });
  }
  btn.addEventListener('click', () => {
    root.classList.toggle('paused');
    try { localStorage.setItem(KEY, root.classList.contains('paused') ? '1' : '0'); } catch (e) { /* ignore */ }
    sync();
  });
  sync();
})();

// Rotating word in the hero headline
(function () {
  const el = document.getElementById('swap');
  if (!el || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const words = ['VS Code', 'Word', 'your inbox'];
  let i = 0;
  setInterval(() => {
    el.classList.add('out');
    setTimeout(() => { i = (i + 1) % words.length; el.textContent = words[i]; el.classList.remove('out'); }, 260);
  }, 2600);
})();

// "See how it works": pick an assignment and watch an example run. All content is static and trusted.
(function () {
  const stage = document.getElementById('howStage');
  if (!stage) return;
  const root = document.documentElement;
  const osReduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const still = () => osReduce || root.classList.contains('paused');
  const tabs = [...document.querySelectorAll('.how-tab')];
  const chips = [...document.querySelectorAll('.how-chip')];
  const leftEl = document.getElementById('howLeft'), rightEl = document.getElementById('howRight');
  const noteEl = document.getElementById('howNote'), statusEl = document.getElementById('howStatus');

  const doc = (name, lines) => '<div class="w-light"><div class="w-bar"><s></s><s></s><s></s><span>' + name + '</span></div><div class="w-body">' + lines.map(l => '<p class="w-txt">' + l + '</p>').join('') + '<div class="w-line" style="width:70%"></div><div class="w-line" style="width:88%"></div></div></div>';
  const tools = list0 => { const list = list0.filter(x => x[1]); return '<div class="w-light"><div class="w-bar"><s></s><s></s><s></s><span>Tools you can approve</span></div><div class="w-body">' + list.map(([n, on, why]) => '<div class="w-row"><span><b>' + n + '</b> <span style="color:var(--muted)">' + why + '</span></span><i class="w-tg' + (on ? ' on' : '') + '"></i></div>').join('') + '</div></div>'; };
  const code = (file, body) => '<div class="w-win"><div class="w-bar"><s></s><s></s><s></s><span>' + file + '</span></div><pre>' + body + '</pre></div>';
  const word = (title, ask) => '<div class="w-light"><div class="w-bar"><s></s><s></s><s></s><span>essay.docx · Word</span></div><div class="w-body"><h5>' + title + '</h5><div class="w-line" style="width:96%"></div><div class="w-line" style="width:90%"></div><div class="w-line" style="width:94%"></div><div class="w-line" style="width:60%"></div>' + ask + '</div></div>';
  const mail = (to, subj, body) => '<div class="w-light"><div class="w-bar"><s></s><s></s><s></s><span>New message · draft</span></div><div class="w-body"><div class="w-field"><b>To</b><span>' + to + '</span></div><div class="w-field"><b>Subject</b><span>' + subj + '</span></div><p class="w-txt" style="margin-top:12px">' + body + '</p></div></div>';
  const report = rows => '<div class="w-light"><div class="w-bar"><s></s><s></s><s></s><span>Validation report</span></div><div class="w-body">' + rows.map(([t, b, cls]) => '<div class="w-row"><span>' + t + '</span><span class="w-badge ' + cls + '">' + b + '</span></div>').join('') + '</div></div>';

  const SC = [
    { tag: 'ASSIGNMENT · CODING QUIZ', title: 'Python basics', items: ['Q1 is_palindrome(s)', 'Q2 fizzbuzz(n)', 'Q3 word_count(text)'],
      notes: ['TaskWrite reads the file and lists every task as a checklist you can edit.', 'You choose the tools it may use. Nothing runs until you approve.', 'It saves the code and asks VS Code to open it. If it gets stuck, it asks you.', 'Every task is checked. You review the code, run your own tests and decide.'],
      status: ['✓ read coding_assessment.pdf: 3 coding tasks', '? you approved model, vscode and code_runner', '✓ saved q2_solution.py, asked VS Code to open it', '✓ code ran without errors (correctness not tested)'],
      screens: [
        doc('coding_assessment.pdf', ['1. Write is_palindrome(s) that ignores case and punctuation.', '2. Write fizzbuzz(n) that returns a list of strings.', '3. Write word_count(text) that returns a dictionary.']),
        tools([['model', 1, 'writes the code'], ['vscode', 1, 'saves and opens it'], ['code_runner', 1, 'runs it once'], ['email', 0, 'not needed']]),
        code('q2_solution.py · VS Code', '<span class="k">def</span> fizzbuzz(n):\n    out = []\n    <span class="k">for</span> i <span class="k">in</span> range(1, n + 1):\n        <span class="k">if</span> i % 15 == 0:\n            out.append(<span class="s">"FizzBuzz"</span>)\n        <span class="k">elif</span> i % 3 == 0:\n            out.append(<span class="s">"Fizz"</span>)\n        <span class="c"># ...</span>'),
        report([['Q1 is_palindrome', 'answered · not verified', 'b-an'], ['Q2 fizzbuzz', 'answered · not verified', 'b-an'], ['Q3 word_count', 'answered · not verified', 'b-an'], ['Code ran without errors', 'tested: no', 'b-un']]) ] },
    { tag: 'ESSAY PROMPT', title: 'Remote learning', items: ['Max 500 words', 'Argue one side', 'Cite two sources'],
      notes: ['TaskWrite reads the prompt and lists every rule, including the word limit.', 'You choose the tools. For an essay that is the model and Word.', 'It drafts the essay. If it needs your opinion, it pauses and asks you.', 'The word limit is checked for real. Check the sources yourself, since it cannot verify them.'],
      status: ['✓ read essay_prompt.docx: 1 essay, 500 words max', '? you approved model and word', '✓ saved essay.docx, asked Word to open it', '✓ word count 468 of 500'],
      screens: [
        doc('essay_prompt.docx', ['Write an essay of at most 500 words arguing one side on remote learning.', 'Cite two sources.']),
        tools([['model', 1, 'drafts the essay'], ['word', 1, 'saves and opens it'], ['email', 0, 'not needed'], ['vscode', 0, 'not needed']]),
        word('Remote learning is worth keeping', '<div class="w-ask">? Which side should the essay argue?<br><b>you: remote learning is worth keeping</b></div>'),
        report([['Word limit (max 500)', '468 words · verified', 'b-ok'], ['Argues one side', 'answered · not verified', 'b-an'], ['Cite two sources', 'check yourself', 'b-un']]) ] },
    { tag: 'EMAIL TASK', title: 'Extension request', items: ['To ms.rao@school.edu', 'Ask for two more days', 'Polite tone'],
      notes: ['TaskWrite reads the task and finds the address in it.', 'You approve the email tool, which only prepares a draft.', 'It writes the email and opens a draft in your mail app. It never sends.', 'You review the draft and press Send yourself. TaskWrite cannot send it or tell you whether it was sent.'],
      status: ['✓ read email_task.txt: write to ms.rao@school.edu', '? you approved model and email (draft only)', '✓ draft opened in your mail app', '? you review it and press Send'],
      screens: [
        doc('email_task.txt', ['Email ms.rao@school.edu and ask for a two-day extension on the lab report.']),
        tools([['model', 1, 'writes the email'], ['email', 1, 'prepares a draft only'], ['word', 0, 'not needed'], ['vscode', 0, 'not needed']]),
        mail('ms.rao@school.edu', 'Extension request: lab report', 'Hello Ms. Rao, could I please have two more days to finish the lab report? Thank you for considering it.'),
        report([['Address found in the task', 'verified', 'b-ok'], ['Draft written', 'answered · not verified', 'b-an'], ['Sent', 'you press Send', 'b-un']]) ] }
  ];

  let sc = 0, step = 0, timer = null, userTook = false;
  const HOLD_MS = 3200, FADE_MS = 350;

  function renderLeft() {
    const d = SC[sc];
    const doneCount = step >= 3 ? 3 : step >= 2 ? 2 : 0;
    leftEl.innerHTML = '<span class="tag">' + d.tag + '</span><h4>' + d.title + '</h4>' + d.items.map((t, i) => '<div class="it' + (i < doneCount ? ' done' : '') + '"><span class="box"></span>' + t + '</div>').join('') + '<div class="pbar"><i style="width:' + ((step + 1) * 25) + '%"></i></div>';
  }
  function paint(animate) {
    const d = SC[sc];
    chips.forEach((c, i) => { c.classList.toggle('done', i < step); c.classList.toggle('run', i === step); });
    const apply = () => { rightEl.innerHTML = d.screens[step]; rightEl.classList.remove('out'); };
    if (animate && !still()) { rightEl.classList.add('out'); setTimeout(apply, FADE_MS); } else apply();
    renderLeft();
    noteEl.textContent = d.notes[step];
    statusEl.textContent = d.status[step];
    if (window.__howFit) setTimeout(() => window.__howFit(), FADE_MS + 30);
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  function play() {
    stop();
    if (still() || userTook) return;
    timer = setInterval(() => { step = (step + 1) % 4; paint(true); }, HOLD_MS);
  }
  tabs.forEach(t => t.addEventListener('click', () => {
    sc = Number(t.dataset.i); step = still() ? 3 : 0; userTook = false;
    tabs.forEach(x => { x.classList.toggle('on', x === t); x.setAttribute('aria-selected', String(x === t)); });
    paint(true); play();
  }));
  chips.forEach(c => c.addEventListener('click', () => { step = Number(c.dataset.s); userTook = true; stop(); paint(true); }));

  // Scale the whole block so nav + demo + footer fill exactly one screen (never smaller than 0.5, never huge).
  const wrap = document.querySelector('.how .wrap');
  function fit() {
    if (!wrap) return;
    if (window.innerWidth <= 900) { wrap.style.zoom = ''; return; }   // stacked layout: natural size, the page scrolls
    wrap.style.zoom = '1';
    const nav = document.querySelector('header.nav').offsetHeight;
    const foot = (document.querySelector('footer') || { offsetHeight: 0 }).offsetHeight;
    document.documentElement.style.setProperty('--navh', nav + 'px');
    document.documentElement.style.setProperty('--footh', foot + 'px');
    const how = document.querySelector('.how'); const cs = getComputedStyle(how);
    const need = wrap.offsetHeight + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + 18;
    const room = window.innerHeight - nav - foot;
    const byHeight = room / need;
    const byWidth = (window.innerWidth * 0.92) / 1060;
    const z = Math.max(0.5, Math.min(byHeight, byWidth, 1.8));
    wrap.style.zoom = z.toFixed(3);
  }
  let fitQueued = false;
  const queueFit = () => { if (fitQueued) return; fitQueued = true; requestAnimationFrame(() => { fitQueued = false; fit(); }); };
  window.addEventListener('resize', queueFit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
  window.__howFit = fit;

  step = still() ? 3 : 0;
  paint(false); play();
  queueFit();
})();

// Home hero: the question and corner cards rotate on their own timer, plus cursor parallax.
// All text is static and trusted; nothing from the page or the user is inserted as HTML.
(function () {
  const ask = document.getElementById('ask');
  if (!ask) return;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const hero = document.querySelector('.hero-tpl');
  const slots = ['.f-assign', '.f-plan', '.f-code', '.f-mail', '.f-tools', '.f-ask', '.f-chip1', '.f-chip2'].map(sel => document.querySelector(sel));
  const meter = n => '<div class="meter"><span class="bars">' + Array.from({ length: 8 }, (_, k) => '<i' + (k < n ? '' : ' class="off"') + '></i>').join('') + '</span><em>' + n + ' / 8</em> step budget</div>';
  const chk = (on, t) => '<div class="check"><i' + (on ? ' class="on"' : '') + '></i>' + t + '</div>';
  const scenarios = [
    { q: 'Have a coding assessment to complete?',
      assign: '<div class="face"><span class="tag">ASSIGNMENT · CODING QUIZ</span><h4>Python basics</h4>' + chk(1, 'Q1 is_palindrome(s)') + chk(1, 'Q2 fizzbuzz(n)') + chk(0, 'Q3 word_count(text)') + '</div>',
      plan: '<span class="tag">PLAN · STEP 2 OF 4</span><h4>Write fizzbuzz() in VS Code</h4><p>Needs <b>model</b> and <b>vscode</b>. Nothing runs until you say yes.</p>' + meter(4),
      code: '<div class="bar"><s></s><s></s><s></s>&nbsp;q2_solution.py</div><pre><span class="k">def</span> fizzbuzz(n):\n    out = []\n    <span class="k">for</span> i <span class="k">in</span> range(1, n + 1):\n    <span class="c">    # ...checked, then opened</span></pre>',
      mail: '<span class="tag">CODE RUNNER</span><div class="row"><span class="label">Result</span><span>ran without errors</span></div><div class="row"><span class="label">Correct?</span><span>not tested</span></div><span class="go">Review it in VS Code</span>' },
    { q: 'Have an essay to write?',
      assign: '<div class="face"><span class="tag">ESSAY PROMPT</span><h4>Remote learning</h4>' + chk(1, 'Max 500 words') + chk(1, 'Argue one side') + chk(0, 'Cite two sources') + '</div>',
      plan: '<span class="tag">PLAN · STEP 2 OF 3</span><h4>Draft the essay for Word</h4><p>Needs <b>model</b> and <b>word</b>. It asks you which side to argue.</p>' + meter(3),
      code: '<div class="bar"><s></s><s></s><s></s>&nbsp;essay.docx</div><pre class="plain">Remote learning widened access\nfor students far from campus,\nbut it works best when it is\n<span class="c">...drafted, then opened in Word</span></pre>',
      mail: '<span class="tag">WORD COUNT</span><div class="row"><span class="label">Words</span><span>468 / 500</span></div><div class="row"><span class="label">Rule</span><span>max 500: met</span></div><span class="go">Open in Word</span>' },
    { q: 'Have an email to send?',
      assign: '<div class="face"><span class="tag">EMAIL TASK</span><h4>Extension request</h4>' + chk(1, 'To ms.rao@school.edu') + chk(1, 'Ask for two more days') + chk(0, 'Tone: you decide') + '</div>',
      plan: '<span class="tag">PLAN · STEP 1 OF 2</span><h4>Write the email</h4><p>Needs <b>model</b> and <b>email</b>. It only prepares a draft.</p>' + meter(1),
      code: '<div class="bar"><s></s><s></s><s></s>&nbsp;email body</div><pre class="plain">Hello Ms. Rao,\n\nCould I have two more days\nfor the lab report? <span class="c">...</span></pre>',
      mail: '<span class="tag red">EMAIL · DRAFT ONLY</span><div class="row"><span class="label">To</span><span>ms.rao@school.edu</span></div><div class="row"><span class="label">Send</span><span>you press it</span></div><span class="go">Open draft in my mail app</span>' }
  ];
  const tg = (name, on) => '<div class="trow"><span>' + name + '</span><i class="tg' + (on ? ' on' : '') + '"></i></div>';
  const askCardHtml = (q, a) => '<span class="tag">ASKS YOU, NEVER GUESSES</span><p>' + q + '</p><span class="fakein">' + a + '</span>';
  const chip = t => '<i class="ck"></i>' + t;
  const extras = [
    { tools: '<span class="tag">TOOLS · YOU DECIDE</span>' + tg('model', 1) + tg('vscode', 1) + tg('code_runner', 1),
      askCard: askCardHtml('Which function should I write first?', 'is_palindrome'), chip1: chip('code ran without errors'), chip2: chip('saved q2_solution.py, asked VS Code to open it') },
    { tools: '<span class="tag">TOOLS · YOU DECIDE</span>' + tg('model', 1) + tg('word', 1) + tg('email', 0),
      askCard: askCardHtml('Which side should the essay argue?', 'remote learning is worth keeping'), chip1: chip('word count 468 of 500'), chip2: chip('saved essay.docx, asked Word to open it') },
    { tools: '<span class="tag">TOOLS · YOU DECIDE</span>' + tg('model', 1) + tg('email', 1) + tg('vscode', 0),
      askCard: askCardHtml('Which date should I ask for?', 'Friday'), chip1: chip('draft only: you press Send'), chip2: chip('opens in your mail app, never sends') }
  ];
  extras.forEach((e, i) => Object.assign(scenarios[i], e));
  let idx = 0;

  const FADE_MS = 700;   // matches the CSS transition
  function setCards(i) {
    const sc = scenarios[i];
    const html = [sc.assign, sc.plan, sc.code, sc.mail, sc.tools, sc.askCard, sc.chip1, sc.chip2];
    slots.forEach((el, n) => {
      if (!el) return;
      const lag = reduce ? 0 : n * 90;  // a slow cascade: one card at a time, never a whole-screen flash
      setTimeout(() => el.classList.add('swap'), lag);
      setTimeout(() => { el.innerHTML = html[n]; el.classList.remove('swap'); }, lag + (reduce ? 0 : FADE_MS));
    });
  }
  function select(i) {
    idx = i;
    ask.classList.add('out');
    setTimeout(() => { ask.textContent = scenarios[i].q; ask.classList.remove('out'); }, reduce ? 0 : FADE_MS);
    setCards(i);
  }
  if (!reduce) setInterval(() => { if (!document.documentElement.classList.contains('paused')) select((idx + 1) % scenarios.length); }, 6000);

  // cursor parallax on the corner cards (wide screens only; a cheap transform, no layout)
  if (hero && !reduce && window.matchMedia('(min-width: 1181px)').matches) {
    hero.addEventListener('mousemove', e => {
      if (document.documentElement.classList.contains('paused')) return;
      const r = hero.getBoundingClientRect();
      const nx = (e.clientX - r.left) / r.width - 0.5, ny = (e.clientY - r.top) / r.height - 0.5;
      slots.forEach(el => { if (el) { const d = Number(el.dataset.depth || 0); el.style.setProperty('--tx', (-nx * d).toFixed(1) + 'px'); el.style.setProperty('--ty', (-ny * d).toFixed(1) + 'px'); } });
    });
    hero.addEventListener('mouseleave', () => slots.forEach(el => { if (el) { el.style.setProperty('--tx', '0px'); el.style.setProperty('--ty', '0px'); } }));
  }
})();
