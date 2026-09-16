// Runs the bundled chat card in a bare Node VM and prints what it sent.
// Only the parts of the DOM the card touches outside rendering are stubbed;
// rendering is skipped by never assigning `hass` through its setter.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(process.argv[2], "utf8");

class StubElement {
  attachShadow() {
    return {};
  }
}
const registry = new Map();
const context = {
  HTMLElement: StubElement,
  customElements: {
    get: (name) => registry.get(name),
    define: (name, cls) => registry.set(name, cls),
  },
  window: {},
  console: { info() {} },
};
vm.createContext(context);
vm.runInContext(source, context);
const Card = registry.get("claude-chat-card");

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function makeCard(config, replies) {
  const card = new Card();
  const calls = [];
  // A card only asks once built; mark it built without rendering anything.
  card._built = true;
  card._input = { focus() {} };
  card._sendBtn = {};
  card._hass = {
    callService: (_domain, _service, data) => {
      calls.push(data);
      return replies.shift();
    },
  };
  card.setConfig(config);
  return { card, calls };
}

const proposal = {
  response: {
    text: "Turn off the lights?",
    proposal: { summary: "Lights off", intents: [{ intent: "HassTurnOff" }] },
  },
};
const done = { response: { text: "Done." } };
const results = {};

// A proposal made for A is never applied to B after the target changes.
{
  const { card, calls } = makeCard({ config_entry: "A" }, [proposal, done]);
  await card._ask({ prompt: "lights" }, "A");
  const msg = card._messages.at(-1);
  card.setConfig({ config_entry: "B" });
  await card._apply(msg);
  results.retarget_then_apply = { calls, messages: card._messages.length };
}

// Unrelated config changes keep the conversation, and Apply goes to the origin.
{
  const { card, calls } = makeCard({ config_entry: "A" }, [proposal, done]);
  await card._ask({ prompt: "lights" }, "A");
  card.setConfig({ config_entry: "A", title: "Kitchen" });
  await card._apply(card._messages.at(-1));
  results.same_target_apply = { calls };
}

// A reply still on its way when the target changes is dropped.
{
  const late = deferred();
  const { card, calls } = makeCard({ config_entry: "A" }, [late.promise, done]);
  const pending = card._ask({ prompt: "lights" }, "A");
  card.setConfig({ config_entry: "B" });
  late.resolve(proposal);
  await pending;
  const busyAfterLate = card._busy;
  await card._ask({ prompt: "hello" }, card._config.config_entry);
  results.late_reply = {
    calls,
    messages: card._messages.map((m) => ({ entry: m.entry, text: m.text })),
    busyAfterLate,
  };
}

// Without a target nothing extra is sent, for the question or the Apply.
{
  const { card, calls } = makeCard({}, [proposal, done]);
  await card._ask({ prompt: "lights" }, card._config.config_entry);
  await card._apply(card._messages.at(-1));
  results.no_target = { calls };
}

process.stdout.write(JSON.stringify(results));
