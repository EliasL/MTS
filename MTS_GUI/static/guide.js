const detected = /Mac|iPhone|iPad/.test(navigator.platform) ? 'macOS' : /Linux/.test(navigator.platform) ? 'Linux' : 'Unknown OS';
const quote = text => "'" + text.replace(/'/g, "'\\''") + "'";
const here = location.protocol === 'file:' ? decodeURIComponent(location.pathname).replace(/\/InstallationGuide\.html$/, '') : '/path/to/MTS';
document.getElementById('os-badge').textContent = detected;
document.getElementById('launcher-name').textContent = detected === 'macOS' ? 'MTS.app' : 'MTS.desktop';
if (detected === 'macOS') {
  document.getElementById('tools-description').textContent = 'Requires Homebrew. Installs Apple tools if missing; installs or upgrades CMake and OpenMP.';
  document.getElementById('launcher-note').textContent = 'Locally generated, unsigned launcher.';
} else if (detected === 'Linux') {
  document.getElementById('tools-description').textContent = 'Ubuntu/Debian: installs compiler tools, CMake and zlib using apt. Administrator access required.';
  document.getElementById('launcher-note').textContent = 'Allow launching MTS.desktop if prompted.';
}
function updateCommand() {const mode = document.querySelector('[name=mode]:checked').value; document.getElementById('setup-command').value = 'bash ' + quote(here + '/setup.sh') + (mode ? ' ' + mode : '');}
document.querySelectorAll('[name=mode]').forEach(el => el.addEventListener('change', updateCommand)); updateCommand();
document.getElementById('copy-command').onclick = async event => {
  const input = document.getElementById('setup-command'); input.select();
  try {await navigator.clipboard.writeText(input.value); event.target.textContent = 'Copied';}
  catch {event.target.textContent = 'Copy selected text';}
  setTimeout(() => event.target.textContent = 'Copy', 2500);
};
