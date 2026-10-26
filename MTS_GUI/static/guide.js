const detected = /Mac|iPhone|iPad/.test(navigator.platform) ? 'macOS' : /Linux/.test(navigator.platform) ? 'Linux' : 'Unknown OS';
const quote = text => "'" + text.replace(/'/g, "'\\''") + "'";
const here = location.protocol === 'file:' ? decodeURIComponent(location.pathname).replace(/\/InstallationGuide\.html$/, '') : '/path/to/MTS';
document.getElementById('os-badge').textContent = detected;
document.getElementById('launcher-name').textContent = detected === 'macOS' ? 'MTS.app' : 'MTS.desktop';
if (detected === 'macOS') {
  document.getElementById('terminal-instructions').textContent = 'Open Terminal (Applications → Utilities, or search for Terminal with Spotlight), then paste this command. You do not need to navigate to the folder first.';
  document.getElementById('tools-description').textContent = 'Request Apple Command Line Tools if missing, then use Homebrew to install CMake and OpenMP. If Homebrew is not installed, follow the instructions at brew.sh and rerun this command. Apple installation dialogs may require you to finish setup and run the command again.';
  document.getElementById('launcher-note').textContent = 'MTS.app is generated locally by setup. Downloaded applications may be subject to macOS Gatekeeper approval; this development launcher is not a signed, notarized release.';
} else if (detected === 'Linux') {
  document.getElementById('terminal-instructions').textContent = 'Open your terminal application and paste this command. You do not need to navigate to the folder first.';
  document.getElementById('launcher-note').textContent = 'Your desktop environment may require you to mark MTS.desktop as trusted or choose “Allow Launching” once. You can always start it using bash setup.sh --start.';
}
function updateCommand() {const mode = document.querySelector('[name=mode]:checked').value; document.getElementById('setup-command').value = 'bash ' + quote(here + '/setup.sh') + (mode ? ' ' + mode : '');}
document.querySelectorAll('[name=mode]').forEach(el => el.addEventListener('change', updateCommand)); updateCommand();
document.getElementById('copy-command').onclick = async event => {
  const input = document.getElementById('setup-command'); input.select();
  try {await navigator.clipboard.writeText(input.value); event.target.textContent = 'Copied';}
  catch {event.target.textContent = 'Selected — copy now';}
  setTimeout(() => event.target.textContent = 'Copy command', 2500);
};
