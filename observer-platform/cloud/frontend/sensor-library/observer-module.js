"use strict";
(function () {
  const back = document.getElementById('observer-return');
  if (!back) return;
  const selected = new URLSearchParams(window.location.search).get('robot');
  const url = new URL('/', window.location.href);
  if (selected) url.searchParams.set('robot', selected);
  url.hash = 'robot/sensors';
  back.href = url.pathname + url.search + url.hash;
})();
