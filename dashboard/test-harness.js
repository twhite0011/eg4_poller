/* Faithful DOM stub.
 *
 * The important detail: getElementById returns NULL for ids that were never
 * rendered, exactly as a browser does. The earlier harness auto-created any
 * id asked for, which hid a paint() crash on the window rows -- and that
 * crash was why the inverter clock never appeared.
 *
 * innerHTML is a setter that harvests ids, so elements produced by generated
 * markup become resolvable at the same moment they would in a browser.
 */
const fs = require("fs");

function makeHarness(file){
  const html = fs.readFileSync(file, "utf8");
  const src  = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];
  const store = {}, pub = [];
  const known = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));

  function mk(id){
    const o = { _id:id, textContent:"", className:"", style:{}, value:"",
                placeholder:"", disabled:false, title:"", _cls:new Set(), _h:{} };
    let _html = "";
    Object.defineProperty(o, "innerHTML", {
      get: () => _html,
      set: v => { _html = v;
        for (const m of String(v).matchAll(/id="([^"]+)"/g)) known.add(m[1]); },
    });
    o.classList = { add:c=>o._cls.add(c), remove:c=>o._cls.delete(c),
      toggle:(c,v)=>v?o._cls.add(c):o._cls.delete(c), contains:c=>o._cls.has(c) };
    o.addEventListener = (e,f) => { (o._h[e] = o._h[e]||[]).push(f); };
    o.dispatch = e => (o._h[e]||[]).forEach(f => f());
    o.focus = () => o.dispatch("focus");
    o.insertAdjacentHTML = (p,s) => { o.innerHTML = _html + s; };
    o.showModal = () => { o._open = true; };
    o.close = () => { o._open = false; };
    o.closest = () => store["__row_"+id] || (store["__row_"+id] = mk("__row_"+id));
    return o;
  }
  const get = id => store[id] || (known.has(id) ? (store[id] = mk(id)) : null);

  global.document = { getElementById:get, activeElement:null };
  global.window = { addEventListener(){} };
  global.location = { protocol:"https:", host:"x" };
  global.setTimeout = () => 0;
  global.clearTimeout = () => {};
  let onMsg = null;
  global.mqtt = { connect: () => ({ on(e,f){ if(e==="message") onMsg=f; },
    subscribe(){}, publish(t,p){ pub.push({t, p:JSON.parse(p)}); } }) };

  const api = new Function(src +
    "\nreturn {inputs,GROUPS,onEdit,paint,saveGroup,discardGroup,dirtyIn," +
    "setArmed:v=>{armed=v},getState:()=>state};")();
  return { api, store, pub, get,
           send:(t,o)=>onMsg(t, Buffer.from(JSON.stringify(o))) };
}
module.exports = { makeHarness };
