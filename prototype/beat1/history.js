(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Beat1History = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var LIMIT = 10;

  function clean(message) {
    if (!message || (message.role !== 'user' && message.role !== 'assistant')) return null;
    var text = typeof message.text === 'string' ? message.text.trim().slice(0, 1000) : '';
    return text ? { role: message.role, text: text } : null;
  }

  function trim(history) {
    while (history.length > LIMIT) history.shift();
    return history;
  }

  function snapshot(history) {
    return (history || []).slice(-LIMIT).map(clean).filter(Boolean);
  }

  // Capture the prior rolling window, then retain the accepted user message
  // immediately. If the network/provider fails, the next request still carries
  // what the UI promised was not lost.
  function acceptUser(history, text) {
    var prior = snapshot(history);
    var message = clean({ role: 'user', text: text });
    if (message) history.push(message);
    trim(history);
    return prior;
  }

  function acceptReply(history, text) {
    var message = clean({ role: 'assistant', text: text });
    if (message) history.push(message);
    trim(history);
  }

  return { LIMIT: LIMIT, snapshot: snapshot, acceptUser: acceptUser, acceptReply: acceptReply };
}));
