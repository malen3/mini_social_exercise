/*
 * Tone Coach (Assignment 2, Kraut & Resnick Chapter 4, Design Claim 20)
 *
 * The "waiter": when the user stops typing, it takes the text to /api/tone-check,
 * brings back the answer of analyze_text() and draws it in the panel under the form.
 * When the user clicks "Post", it checks the text one last time and, if there are
 * problems, opens the "Before you post…" window.
 *
 * Security: the text is always added with textContent (as plain text, never as HTML).
 */

// Take a text to the mailbox and bring back the answer of analyze_text()
// (used twice: while typing and when clicking "Post", so it is a function)
function analyze(text) {
  // POST, as JSON: {"text": "..."}
  return fetch('/api/tone-check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text })
  })
    // The answer comes as JSON: turn it into a JavaScript object
    .then(function (response) { return response.json(); });
}

// Fill a list (<ul>) with the problems of the message: WHY it would be changed
// (used twice: in the panel and in the "Before you post…" window)
function fillIssues(list, result) {
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
}

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

  // The list of problems (a <ul> list, one <li> per problem)
  var list = document.createElement('ul');
  list.className = 'tc-issues';
  fillIssues(list, result);

  // If the list is empty, there is nothing to fix
  if (list.children.length === 0) {
    var ok = document.createElement('li');
    ok.className = 'tc-ok';
    ok.textContent = '✓ Looks good!';
    list.appendChild(ok);
  }
  panel.appendChild(list);
}

// Open the "Before you post…" window (it is in base.html.j2) with the preview and the problems
// (form and textarea: the form that is being sent, so the buttons know what to send or edit)
function openConfirm(form, textarea, result) {
  // The preview: the same as in the panel
  var preview = document.getElementById('tcConfirmPreview');
  preview.className = result.removed ? 'tc-preview tc-removed' : 'tc-preview';
  preview.textContent = result.moderated;

  // The list of problems: empty it first, then fill it again
  var reasons = document.getElementById('tcConfirmReasons');
  reasons.replaceChildren();
  fillIssues(reasons, result);

  // The "Before you post…" window, as a Bootstrap object
  var confirmEl = document.getElementById('tcConfirmModal');
  var modal = bootstrap.Modal.getOrCreateInstance(confirmEl);

  // Comments are written INSIDE another window, and Bootstrap cannot show a window on top of another one.
  // closest('.modal') = "the window this form is in" (null for the post form, which is not in a window)
  var parentEl = form.closest('.modal');
  var parentModal = parentEl ? bootstrap.Modal.getOrCreateInstance(parentEl) : null;

  if (parentModal) {
    // A comment: first hide the comment window, and when it is completely hidden, open "Before you post…"
    // ('hidden.bs.modal' = Bootstrap's event "this window has finished hiding"; once = only this time)
    parentEl.addEventListener('hidden.bs.modal', function () { modal.show(); }, { once: true });
    parentModal.hide();
  } else {
    // The post form: simply open the window
    modal.show();
  }

  // What the two buttons do (onclick = "when this button is clicked")
  // (onclick, and not addEventListener, so every new opening REPLACES the old action instead of adding one more)

  // "Edit my message": close the window and put the cursor back in the text box
  document.getElementById('tcEditMessage').onclick = function () {
    if (parentModal) {
      // A comment: when "Before you post…" has finished hiding, open the comment window again,
      // and when that one is completely open ('shown.bs.modal'), put the cursor in the text box
      confirmEl.addEventListener('hidden.bs.modal', function () {
        parentEl.addEventListener('shown.bs.modal', function () { textarea.focus(); }, { once: true });
        parentModal.show();
      }, { once: true });
      modal.hide();
    } else {
      modal.hide();
      textarea.focus();
    }
  };

  // "Post anyway": close the window and send the form as it is
  document.getElementById('tcPostAnyway').onclick = function () {
    modal.hide();
    // Send the form without asking again
    form.submit();
  };
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

    // ----- 1. While typing: the live panel -----
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

        // Ask the brain and draw the answer in the panel
        analyze(textarea.value).then(function (result) { showResult(panel, result); });

      }, 400);
    });

    // ----- 2. When clicking "Post" / "Submit Comment": the last check -----
    // When the form is being sent ("submit")...
    form.addEventListener('submit', function (event) {
      // "Wait!": stop the form, so the message is not sent yet
      event.preventDefault();

      // Ask the brain one last time, with the text as it is now
      analyze(textarea.value).then(function (result) {
        // needs_warning = the text would be censored OR it sounds very negative (see analyze_text)
        if (result.needs_warning) {
          // There are problems: open the "Before you post…" window
          openConfirm(form, textarea, result);
        } else {
          // No problems: send the form now (form.submit() sends it without asking again)
          form.submit();
        }
      }).catch(function () {
        // If the check fails (no internet, server error...), never block the user: send the form
        form.submit();
      });
    });
  });
});
