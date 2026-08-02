'use strict';
//
// Queue editing for node-sonos-http-api.
//
// The upstream project ships no reorder or remove action, but the underlying
// sonos-discovery Player already implements both. This adds the two HTTP
// routes the DJ server needs:
//
//   /{room}/queuemove/{fromIndex}/{toIndex}
//   /{room}/queueremove/{index}
//
// Indices are 1-based, matching what /{room}/queue returns.
//
// Why this lives here rather than in the DJ server: macOS grants Local
// Network access per process, and the launchd-run Python server does not
// have it -- direct UPnP calls to the speaker fail with "no route to host".
// This process already talks to the speaker, so it does have it.
//
// Install:  cp sonos-actions/*.js <node-sonos-http-api>/lib/actions/
//           then restart node-sonos-http-api.

function queuemove(player, values) {
  const from = parseInt(values[0], 10);
  const to = parseInt(values[1], 10);

  if (!Number.isInteger(from) || !Number.isInteger(to) || from < 1 || to < 1) {
    return Promise.reject(new Error('queuemove needs two 1-based indices'));
  }
  if (from === to) {
    return Promise.resolve({ status: 'unchanged' });
  }

  // InsertBefore counts positions in the pre-move numbering, so moving a
  // track downwards has to account for the gap it leaves behind.
  const insertBefore = to > from ? to + 1 : to;
  return player.coordinator.reorderTracksInQueue(from, 1, insertBefore);
}

function queueremove(player, values) {
  const index = parseInt(values[0], 10);
  if (!Number.isInteger(index) || index < 1) {
    return Promise.reject(new Error('queueremove needs a 1-based index'));
  }
  return player.coordinator.removeTrackFromQueue(index);
}

module.exports = function (api) {
  api.registerAction('queuemove', queuemove);
  api.registerAction('queueremove', queueremove);
};
