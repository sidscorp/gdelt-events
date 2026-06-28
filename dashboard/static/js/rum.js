/* RUM: real-user front-end timings -> /api/perf. Native marks always work;
   web-vitals (INP/CLS/LCP) loads async and is optional. */
(function(){
  window.__perf = [];
  window.__lcp = 0; window.__ttaSent = false;
  window.perfMark = function(metric, value){
    try { if (value==null || !isFinite(value)) return;
      window.__perf.push({ metric: metric, value: Math.round(value),
        view: (window.state&&state.view)||'', hours:(window.state&&state.hours)||0 });
    } catch(e){}
  };
  try { new PerformanceObserver(function(l){ var es=l.getEntries(); window.__lcp = es[es.length-1].startTime; })
        .observe({type:'largest-contentful-paint', buffered:true}); } catch(e){}
  window.perfFlush = function(){
    try {
      if (window.__lcp) { window.perfMark('lcp', window.__lcp); window.__lcp = 0; }
      if (!window.__perf.length) return;
      var body = JSON.stringify({samples: window.__perf.splice(0)});
      if (navigator.sendBeacon) navigator.sendBeacon('/api/perf', new Blob([body], {type:'application/json'}));
      else fetch('/api/perf', {method:'POST', headers:{'Content-Type':'application/json'}, body: body, keepalive:true});
    } catch(e){}
  };
  addEventListener('load', function(){
    try {
      var nav = performance.getEntriesByType('navigation')[0];
      if (nav){ window.perfMark('ttfb', nav.responseStart); }
      var fcp = performance.getEntriesByName('first-contentful-paint')[0];
      if (fcp) window.perfMark('fcp', fcp.startTime);
    } catch(e){}
    setTimeout(window.perfFlush, 2500);
  });
  // Register the service worker (instant app-shell open on subsequent loads).
  if ('serviceWorker' in navigator) {
    addEventListener('load', function(){ navigator.serviceWorker.register('/sw.js').catch(function(){}); });
  }
  addEventListener('visibilitychange', function(){ if (document.visibilityState==='hidden') window.perfFlush(); });
  var s=document.createElement('script');
  s.src='https://unpkg.com/web-vitals@4/dist/web-vitals.iife.js'; s.async=true;
  s.onload=function(){ try{
    webVitals.onINP(function(m){window.perfMark('inp', m.value);});
    webVitals.onCLS(function(m){window.perfMark('cls', m.value*1000);});
    webVitals.onLCP(function(m){window.perfMark('lcp_wv', m.value);});
  }catch(e){} };
  document.head.appendChild(s);
})();
