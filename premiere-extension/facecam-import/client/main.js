/* Small offline JSX runner. No Drive credentials, network listeners, or export automation. */
(function () {
  'use strict';
  var fs = require('fs'), path = require('path');
  var cs = new CSInterface();
  var inbox = path.join(process.env.APPDATA, 'Adobe', 'FacecamImport', 'inbox');
  var busy = false;
  var seen = {};
  fs.mkdirSync(inbox, {recursive: true});
  function status(message, error) {
    document.getElementById('status').textContent = message;
    document.getElementById('status').className = error ? 'error' : '';
    document.getElementById('browse').disabled = busy;
  }
  function run(filename, done) {
    if (busy) return;
    if (!fs.existsSync(filename) || path.basename(filename).toLowerCase() !== 'import_project.jsx') {
      status('Choisissez le fichier import_project.jsx du dossier extrait.', true);
      if (done) done(false);
      return;
    }
    busy = true;
    status('Import en cours…\n' + filename);
    cs.evalScript('facecamRun(' + JSON.stringify(filename.replace(/\\/g, '/')) + ')', function (result) {
      busy = false;
      var success = result === 'ok';
      status(success ? 'Projet importé. Vous pouvez commencer le montage.' : 'Échec de l’import : ' + result, !success);
      if (done) done(success);
    });
  }
  document.getElementById('browse').onclick = function () {
    var result = window.cep.fs.showOpenDialog(false, false, 'Choisir import_project.jsx', '', ['jsx']);
    if (result.err === 0 && result.data && result.data.length) run(result.data[0]);
  };
  // Polling also recovers trigger files created while Premiere was closed.
  setInterval(function () {
    if (busy) return;
    var files = fs.readdirSync(inbox).filter(function (name) {return /\.trigger$/.test(name) && !seen[name];});
    if (!files.length) return;
    var name = files.sort()[0], trigger = path.join(inbox, name);
    try {
      var filename = fs.readFileSync(trigger, 'utf8').replace(/^\uFEFF/, '').trim();
      seen[name] = true;
      run(filename, function (success) {
        if (success) fs.unlinkSync(trigger);
        else fs.renameSync(trigger, trigger + '.failed');
      });
    } catch (e) {seen[name] = true; status('Erreur de lecture : ' + e.message, true);}
  }, 800);
}());
