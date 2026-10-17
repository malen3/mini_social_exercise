/*
 * Tone Coach (Assignment 2, Kraut & Resnick Chapter 4, Design Claim 20)
 *
 * The "waiter": when the user stops typing, it takes the text to /api/tone-check,
 * brings back the answer of analyze_text() and draws it in the panel under the form.
 *
 * Security: the text is always added with textContent (as plain text, never as HTML).
 */

// Draw the answer of analyze_text() inside the panel (the empty box of the form)
function showResult(panel, result) {
  // Empty the box (remove the previous answer)
  panel.replaceChildren();
  // Show the box (it starts hidden)
  panel.hidden = false;

  // The top line: a title, then the tone and the risk, each one in a coloured "pill"
  // (className gives a "colour label"; the colours themselves are in static/tone_coach.css)
  var line = document.createElement('div');
  line.className = 'tc-head';
  line.textContent = '🧭 Tone Coach';

  // The tone pill: tc-tone-negative (red), tc-tone-neutral (grey) or tc-tone-positive (green)
  var tone = document.createElement('span');
  tone.className = 'tc-pill tc-tone-' + result.tone;
  tone.textContent = 'Tone: ' + result.tone;
  line.appendChild(tone);

  // The risk pill: tc-risk-none (grey), tc-risk-low (yellow), tc-risk-medium (orange) or tc-risk-high (red)
  // (toLowerCase: "MEDIUM" -> "medium", because the CSS names are in lowercase)
  var risk = document.createElement('span');
  risk.className = 'tc-pill tc-risk-' + result.risk_label.toLowerCase();
  risk.textContent = 'Risk: ' + result.risk_label;
  line.appendChild(risk);
  panel.appendChild(line);

  // A small title for the preview
  var label = document.createElement('div');
  label.className = 'tc-preview-label';
  label.textContent = 'How others will see it:';
  panel.appendChild(label);

  // The preview: the text as the others will see it (with **** and [link removed])
  var preview = document.createElement('div');
  // If the whole message would be removed, the preview is painted red (tc-removed)
  preview.className = result.removed ? 'tc-preview tc-removed' : 'tc-preview';
  preview.textContent = result.moderated;
  panel.appendChild(preview);

  // The list of problems: WHY the message would be changed (a <ul> list, one <li> per problem)
  var list = document.createElement('ul');
  list.className = 'tc-issues';

  // For every problem found by analyze_text() (like "for issue in result['issues']:" in Python)
  result.issues.forEach(function (issue) {
    var message = '';
    if (issue.type === 'removed') {
      message = '⛔ The whole message would be removed (' + issue.reason + ')';
    } else if (issue.type === 'word') {
      message = '⚠ Mild profanity will be masked: ' + issue.text;
    } else if (issue.type === 'link') {
      message = '🔗 Links are not allowed: ' + issue.text;
    } else if (issue.type === 'caps') {
      message = '🔠 Too many capital letters';
    }
    // Create - add the text - put it in the list (the same 3 steps as always)
    var item = document.createElement('li');
    item.textContent = message;
    list.appendChild(item);
  });

  // A negative tone is not a rule, so it is not in result.issues: it is added apart
  if (result.tone === 'negative') {
    var negative = document.createElement('li');
    negative.textContent = '😠 Your message sounds very negative';
    list.appendChild(negative);
  }

  // If the list is empty, there is nothing to fix
  if (list.children.length === 0) {
    var ok = document.createElement('li');
    ok.className = 'tc-ok';
    ok.textContent = '✓ Looks good!';
    list.appendChild(ok);
  }
  panel.appendChild(list);
}

// Wait until the whole page is loaded, so the forms already exist
document.addEventListener('DOMContentLoaded', function () {

  // Find every form with the sticker data-tone-coach
  document.querySelectorAll('form[data-tone-coach]').forEach(function (form) {
    // The text box of this form
    var textarea = form.querySelector('textarea');
    // The empty box of this form (".tc-panel" = the element with the class tc-panel)
    var panel = form.querySelector('.tc-panel');
    // The "alarm clock" of the waiter (see below)
    var timer = null;

    // Every time the user types (or deletes) something in the text box...
    textarea.addEventListener('input', function () {
      // Every new letter cancels the previous alarm and sets a new one for 400 ms,
      // so the text is only sent when the user stops typing for a moment (debounce)
      clearTimeout(timer);
      timer = setTimeout(function () {

        // If the text box is empty, hide the panel and do not ask the server
        if (!textarea.value.trim()) {
          panel.hidden = true;
          return;
        }

        // Take the text to the mailbox (POST, as JSON: {"text": "..."})
        fetch('/api/tone-check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // What the user has written in the text box
          body: JSON.stringify({ text: textarea.value })
        })
          // The answer comes as JSON: turn it into a JavaScript object
          .then(function (response) { return response.json(); })
          // Draw the answer in the panel
          .then(function (result) { showResult(panel, result); });

      }, 400);
    });
  });
});
