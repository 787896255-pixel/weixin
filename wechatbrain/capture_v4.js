// SHA-512 fallback adapted from wechat-decrypt, MIT.
// Direct object capture is independently implemented from static observations.
// See docs/Windows读取连接-0.6.1.md and third_party/wechat-decrypt/LICENSE.
function scanRanges(m, protection, pattern) {
  var hits = [];
  m.enumerateRanges(protection).forEach(function(range) {
    try {
      Memory.scanSync(range.base, range.size, pattern).forEach(function(hit) {
        hits.push({address: hit.address, range: range});
      });
    } catch (e) {
      send("WARN: scan skipped range @ " + range.base + ": " + e.message);
    }
  });
  return hits;
}

function findEntry(lea, rangeBase) {
  for (var b = 0; b < 8192; b++) {
    var a = lea.sub(b);
    if (a.compare(rangeBase) <= 0) break;
    try {
      if (a.sub(1).readU8() === 0xCC) return a;
    } catch (e) {
      send("WARN: entry inspection failed near " + lea + ": " + e.message);
      break;
    }
  }
  return null;
}

// At an x64 SHA-512 entry, rdx is the input block: key XOR 0x36 || 0x36*96.
function checkPad(p, pad) {
  try { var b = new Uint8Array(p.readByteArray(128)); for (var j=32;j<128;j++) if (b[j]!==pad) return null;
    var s=""; for (var k=0;k<32;k++) s+=('0'+(b[k]^pad).toString(16)).slice(-2); return s; } catch(e){ return null; }
}
var seen = Object.create(null);

function publishCandidate(bytes) {
  var value = "";
  for (var i = 0; i < bytes.length; i++) value += ('0' + bytes[i].toString(16)).slice(-2);
  if (bytes.length === 32 && !seen[value]) {
    seen[value] = true;
    // Private Frida IPC only. The Python receiver authenticates against the DB.
    send("KEY:" + value);
  }
}

function installObjectHooks(m) {
  // Two observed x64 function entries. These are instruction signatures, not
  // version-specific RVAs or a bundled vendor DLL. Match within this module only.
  var patterns = [
    "48 89 5c 24 08 48 89 6c 24 10 48 89 74 24 18 48 89 7c 24 20 41 56 48 83 ec 50 41 8b e9 45 8b f0 48 8b f2 48 8b d9 c7 44 24",
    "55 41 57 41 56 56 57 53 48 83 ec 58 48 8d 6c 24 50 48 c7 45 00 fe ff ff ff 44 89 cf 44 89 c3 49 89 d6 48 89 ce 48 89 d1 e8"
  ];
  var entries = Object.create(null);
  patterns.forEach(function(pattern) {
    scanRanges(m, "r-x", pattern).forEach(function(hit) {
      entries[hit.address.toString()] = hit.address;
    });
  });
  var addresses = Object.keys(entries);
  send("direct-key entries matched: " + addresses.length);
  if (addresses.length > 8) {
    send("WARN: direct-key entry matches are ambiguous; using SHA-512 fallback");
    return 0;
  }
  var attached = 0;
  addresses.forEach(function(address) {
    try {
      Interceptor.attach(entries[address], {onEnter: function(args) {
        // Observed x64 layout: argument 2 points to a descriptor. Its length is
        // a uint32 at +0x10; its data pointer is at +8. Pointer reads may fail.
        try {
          var descriptor = args[1];
          if (descriptor.isNull() || descriptor.add(16).readU32() !== 32) return;
          var data = descriptor.add(8).readPointer();
          if (!data.isNull()) publishCandidate(new Uint8Array(data.readByteArray(32)));
        } catch (e) { /* A bad or changing pointer is not a key candidate. */ }
      }});
      attached++;
    } catch (e) {
      send("WARN: direct-key entry could not be connected");
    }
  });
  if (attached) send("hooked " + attached + " direct-key entries");
  return attached;
}

function installSha512Hooks(m) {
  var kh = scanRanges(m, "r--", "22 ae 28 d7 98 2f 8a 42");
  if (!kh.length) throw new Error("SHA512 K-table not found");
  var tables = Object.create(null);
  kh.forEach(function(hit) { tables[hit.address.toString()] = true; });
  send("SHA-512 K-tables found: " + Object.keys(tables).length);

  // Mask REX.R and ModRM.reg to cover all 16 RIP-relative LEA encodings in one pass.
  var leas = scanRanges(m, "r-x", "48 8d 05 : fb ff c7");
  var entries = Object.create(null);
  var matchingXrefs = 0;
  leas.forEach(function(hit) {
    try {
      if (hit.address.add(7).compare(hit.range.base.add(hit.range.size)) > 0) return;
      var target = hit.address.add(7).add(hit.address.add(3).readS32());
      if (!tables[target.toString()]) return;
      matchingXrefs++;
      var entry = findEntry(hit.address, hit.range.base);
      if (entry) entries[entry.toString()] = entry;
      else send("WARN: entry not found for xref @ " + hit.address);
    } catch (e) {
      send("WARN: xref skipped @ " + hit.address + ": " + e.message);
    }
  });
  send("SHA-512 K-table xrefs found: " + matchingXrefs);
  if (!Object.keys(entries).length) throw new Error("SHA512 entries not found from K-table xrefs");

  var attached = 0;
  Object.keys(entries).forEach(function(address) {
    var entry = entries[address];
    try {
      Interceptor.attach(entry, {onEnter: function() {
        var ip = checkPad(this.context.rdx, 0x36);
        if (ip && !seen[ip]) { seen[ip] = true; send("KEY:" + ip); }
      }});
      attached++;
      send("sha512 entry @ " + entry + " (rva " + entry.sub(m.base) + ")");
    } catch (e) {
      send("WARN: hook failed @ " + entry + ": " + e.message);
    }
  });
  if (!attached) throw new Error("no SHA512 entries could be hooked");
  send("hooked " + attached + " SHA-512 entries; waiting for startup PBKDF2 (raw key)...");
}

var installed = false;
send("waiting for Weixin.dll...");
var moduleObserver = Process.attachModuleObserver({onAdded: function(m) {
  if (installed || m.name.toLowerCase() !== "weixin.dll") return;
  installed = true;
  try {
    var direct = installObjectHooks(m);
    try { installSha512Hooks(m); }
    catch (e) {
      if (!direct) throw e;
      send("WARN: SHA-512 fallback unavailable; direct-key capture remains active");
    }
  }
  catch (e) { send("ERR: " + e.message); }
}});
