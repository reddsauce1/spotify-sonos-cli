'use strict';
//
// Atomic relative volume for node-sonos-http-api.
//
//   /{room}/relvolume/{adjustment}      e.g. /Dining%20Room/relvolume/-3
//
// Why this exists. The shipped `volume` action accepts "+3", but resolves it
// in JavaScript before it ever reaches the speaker:
//
//     if (/^[+\-]/.test(level)) level = this.state.volume + parseInt(level);
//     soap.invoke(... SetVolume, level)                       // absolute
//
// That is a read-modify-write against a cached state.volume. Be careful about
// what that does and does not mean: it was NOT observed to lose updates.
// Five concurrent +1 nudges moved the volume by five, and so did a nudge
// issued straight after changing the volume behind node's back. node is
// single-threaded and _setVolume updates the cache synchronously before the
// SOAP call, so there is no window between the read and the write.
//
// What this action buys is therefore not a bug fix but two smaller things:
//
//   1. Correctness by construction. The speaker applies the delta, so there
//      is no cached value to be right or wrong about -- including when the
//      Sonos app has just moved the volume and node's cache has not caught up.
//   2. One round trip instead of two. SetRelativeVolume returns where it
//      landed, so the caller no longer needs a follow-up state read to find
//      out. It also clamps to 0..100 at the device; the arithmetic above
//      clamps only the lower bound.
//
// The template table in sonos-discovery's soap helper is Object.freeze'd, so
// a new SOAP action cannot be registered through it; this posts the envelope
// directly, as musicSearch.js already does for its own requests.
//
// Install:  cp sonos-actions/*.js <node-sonos-http-api>/lib/actions/
//           then restart node-sonos-http-api.

const request = require('request-promise');

const SERVICE = 'urn:schemas-upnp-org:service:RenderingControl:1';

function relvolume(player, values) {
  const adjustment = parseInt(values[0], 10);

  if (!Number.isInteger(adjustment)) {
    return Promise.reject(new Error('relvolume needs a signed integer'));
  }
  if (adjustment < -100 || adjustment > 100) {
    return Promise.reject(new Error('relvolume adjustment must be within -100..100'));
  }
  // Line-in and fixed-output devices reject volume changes outright.
  if (player.outputFixed) {
    return Promise.resolve({ status: 'fixed-output' });
  }

  const body =
    '<?xml version="1.0"?>' +
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"' +
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>' +
    `<u:SetRelativeVolume xmlns:u="${SERVICE}">` +
    '<InstanceID>0</InstanceID><Channel>Master</Channel>' +
    `<Adjustment>${adjustment}</Adjustment>` +
    '</u:SetRelativeVolume></s:Body></s:Envelope>';

  return request({
    method: 'POST',
    uri: `${player.baseUrl}/MediaRenderer/RenderingControl/Control`,
    headers: {
      'Content-Type': 'text/xml; charset="utf-8"',
      SOAPACTION: `"${SERVICE}#SetRelativeVolume"`,
    },
    body
  }).then((response) => {
    // The speaker reports the volume it settled on, which is what the caller
    // actually wants to know -- it differs from the arithmetic whenever the
    // clamp bites or something else moved the volume in between.
    const match = /<NewVolume>(\d+)<\/NewVolume>/.exec(response);
    return {
      status: 'success',
      newVolume: match ? parseInt(match[1], 10) : null
    };
  });
}

module.exports = function (api) {
  api.registerAction('relvolume', relvolume);
};
