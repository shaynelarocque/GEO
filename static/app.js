function switchTab(name) {
    document.querySelectorAll('.tab-content').forEach(function(el) {
        el.style.display = 'none';
    });
    document.querySelectorAll('.tab').forEach(function(el) {
        el.classList.remove('active');
    });
    document.getElementById('tab-' + name).style.display = 'block';
    // Find the button that matches this tab
    document.querySelectorAll('.tab').forEach(function(btn) {
        if (btn.getAttribute('onclick') === "switchTab('" + name + "')") {
            btn.classList.add('active');
        }
    });
}

// Toggle expandable log entry details
function toggleLogDetails(btn) {
    var details = btn.parentElement.querySelector('.log-details');
    if (details.style.display === 'none') {
        details.style.display = 'block';
        btn.innerHTML = '&#x25BC;';  // down arrow
        btn.classList.add('expanded');
    } else {
        details.style.display = 'none';
        btn.innerHTML = '&#x25B6;';  // right arrow
        btn.classList.remove('expanded');
    }
}

// Auto-scroll the agent log as new entries appear
var logEl = document.getElementById('log-stream');
if (logEl) {
    var observer = new MutationObserver(function() {
        logEl.scrollTop = logEl.scrollHeight;
    });
    observer.observe(logEl, { childList: true });
}

// Escape HTML for safe insertion
function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

// Submit human input response to the agent mid-run
function submitHumanInput(event, requestId) {
    event.preventDefault();
    var form = event.target;
    var textarea = form.querySelector('textarea');
    var response = textarea.value.trim();
    if (!response) return;

    var btn = form.querySelector('button');
    btn.disabled = true;
    btn.textContent = 'Sending...';

    var appId = window.location.pathname.split('/results/')[1];
    var formData = new FormData();
    formData.append('response', response);

    fetch('/api/input/' + appId + '/' + requestId, {
        method: 'POST',
        body: formData,
    })
    .then(function(resp) { return resp.text(); })
    .then(function(html) {
        var card = document.getElementById('input-' + requestId);
        if (card) {
            card.innerHTML = html;
            card.classList.add('human-input-answered');
        }
    })
    .catch(function() {
        btn.disabled = false;
        btn.textContent = 'Send to Agent';
    });
}
