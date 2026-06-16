function checkStrength(val) {
  var rules = [
    ['rule-len',     val.length >= 8],
    ['rule-upper',   /[A-Z]/.test(val)],
    ['rule-lower',   /[a-z]/.test(val)],
    ['rule-num',     /[0-9]/.test(val)],
    ['rule-special', /[^A-Za-z0-9]/.test(val)],
  ];
  rules.forEach(function(r) {
    var el = document.getElementById(r[0]);
    if (!el) return;
    el.style.color = r[1] ? 'var(--success)' : 'var(--text-muted)';
    el.style.fontWeight = r[1] ? '600' : '';
  });
}
