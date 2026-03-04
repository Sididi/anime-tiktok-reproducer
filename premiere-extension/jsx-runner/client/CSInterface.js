/**
 * CSInterface - Adobe CEP 12.x
 *
 * Minimal implementation covering the APIs used by JSX Runner.
 * Based on Adobe's open-source CSInterface.js from CEP-Resources.
 * See: https://github.com/Adobe-CEP/CEP-Resources/tree/master/CEP_12.x
 *
 * If you need the full library, replace this file with the official one.
 */

/* jshint ignore:start */

var SystemPath = {
    USER_DATA: "userData",
    COMMON_FILES: "commonFiles",
    MY_DOCUMENTS: "myDocuments",
    APPLICATION: "application",
    EXTENSION: "extension",
    HOST_APPLICATION: "hostApplication"
};

function CSInterface() {}

/**
 * Evaluate ExtendScript in the host application.
 * @param {string} script - ExtendScript code to evaluate
 * @param {function} [callback] - Optional callback with result
 */
CSInterface.prototype.evalScript = function (script, callback) {
    if (callback === null || callback === undefined) {
        callback = function () {};
    }
    window.__adobe_cep__.evalScript(script, callback);
};

/**
 * Get the host environment information.
 * @returns {object} Host environment info
 */
CSInterface.prototype.getHostEnvironment = function () {
    var env = window.__adobe_cep__.getHostEnvironment();
    return typeof env === "string" ? JSON.parse(env) : env;
};

/**
 * Get system path.
 * @param {string} pathType - One of SystemPath constants
 * @returns {string} The resolved system path
 */
CSInterface.prototype.getSystemPath = function (pathType) {
    var path = decodeURI(window.__adobe_cep__.getSystemPath(pathType));
    // Normalize Windows backslashes
    return path.replace(/file:\/{1,3}/, "");
};

/**
 * Add an event listener for CEP events.
 * @param {string} type - Event type
 * @param {function} listener - Callback function
 * @param {object} [obj] - Optional scope
 */
CSInterface.prototype.addEventListener = function (type, listener, obj) {
    window.__adobe_cep__.addEventListener(type, listener, obj);
};

/**
 * Remove an event listener.
 * @param {string} type - Event type
 * @param {function} listener - Callback function
 * @param {object} [obj] - Optional scope
 */
CSInterface.prototype.removeEventListener = function (type, listener, obj) {
    window.__adobe_cep__.removeEventListener(type, listener, obj);
};

/**
 * Open a URL in the default browser.
 * @param {string} url - URL to open
 */
CSInterface.prototype.openURLInDefaultBrowser = function (url) {
    if (window.cep) {
        window.cep.util.openURLInDefaultBrowser(url);
    }
};

/* jshint ignore:end */
