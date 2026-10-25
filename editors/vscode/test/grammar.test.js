// Tokenizes every Nexo program of the repository with the grammar and
// checks the scopes of a few lines.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const oniguruma = require("vscode-oniguruma");
const textmate = require("vscode-textmate");

const root = path.resolve(__dirname, "..", "..", "..");
const wasm = fs.readFileSync(require.resolve("vscode-oniguruma/release/onig.wasm")).buffer;

async function grammar() {
  await oniguruma.loadWASM(wasm);
  const registry = new textmate.Registry({
    onigLib: Promise.resolve({
      createOnigScanner: (sources) => new oniguruma.OnigScanner(sources),
      createOnigString: (s) => new oniguruma.OnigString(s),
    }),
    loadGrammar: async () => {
      const file = path.join(__dirname, "..", "syntaxes", "nexo.tmLanguage.json");
      return textmate.parseRawGrammar(fs.readFileSync(file, "utf8"), file);
    },
  });
  return registry.loadGrammar("source.nexo");
}

function tokenize(g, text) {
  let state = textmate.INITIAL;
  const out = [];
  for (const line of text.split("\n")) {
    const r = g.tokenizeLine(line, state);
    for (const t of r.tokens) {
      out.push({ text: line.slice(t.startIndex, t.endIndex), scopes: t.scopes });
    }
    state = r.ruleStack;
  }
  return out;
}

function scopeOf(tokens, text) {
  const t = tokens.find((x) => x.text === text);
  assert.ok(t, `no token "${text}" in ${JSON.stringify(tokens.map((x) => x.text))}`);
  return t.scopes[t.scopes.length - 1];
}

function programs(dir, found = []) {
  for (const name of fs.readdirSync(dir)) {
    if (name.startsWith(".") || name === "node_modules" || name === "target") continue;
    const full = path.join(dir, name);
    if (fs.statSync(full).isDirectory()) programs(full, found);
    else if (name.endsWith(".nexo")) found.push(full);
  }
  return found;
}

(async () => {
  const g = await grammar();
  const files = programs(root);
  for (const f of files) {
    tokenize(g, fs.readFileSync(f, "utf8"));
  }
  const cases = [
    ['fn total(prices: list[float]) -> float:', [["fn", "storage.type.function.nexo"], ["total", "entity.name.function.nexo"], ["list", "support.type.builtin.nexo"]]],
    ['type Point:', [["type", "storage.type.nexo"], ["Point", "entity.name.type.nexo"]]],
    ['vat = invoice * 21%  # 25.20 EUR', [["21", "constant.numeric.nexo"], ["%", "constant.numeric.unit.nexo"], ["# 25.20 EUR", "comment.line.number-sign.nexo"]]],
    ['price = 19.99 EUR', [["19.99", "constant.numeric.nexo"], ["EUR", "constant.numeric.unit.nexo"]]],
    ['print("Hello {name}, {{x}}")', [["print", "support.function.builtin.nexo"], ["name", "source.nexo.embedded"], ["{{", "constant.character.escape.nexo"]]],
    ['  |> where amount > 0', [["|>", "keyword.operator.pipeline.nexo"], ["where", "keyword.other.pipeline.nexo"]]],
    ['  |> sort by amount desc', [["sort", "keyword.other.pipeline.nexo"], ["by", "keyword.other.pipeline.nexo"], ["desc", "keyword.other.pipeline.nexo"]]],
    ['## Adds two numbers.', [["## Adds two numbers.", "comment.line.documentation.nexo"]]],
    ['else e is NotFound:', [["else", "keyword.control.nexo"], ["is", "keyword.other.nexo"], ["NotFound", "entity.name.type.nexo"]]],
    ['email: str? = none', [["none", "constant.language.nexo"], ["?", "keyword.operator.error.nexo"]]],
    ['x = 10 % 3', [["%", "keyword.operator.nexo"]]],
    ['server api:', [["server", "keyword.other.module.nexo"]]],
    ['ai models:', [["ai", "keyword.other.module.nexo"], ["models", "keyword.other.module.nexo"]]],
    ['    get "/users/{id}"(id: int):', [["get", "keyword.other.route.nexo"]]],
    ['    fake ask returns "positive"', [["fake", "keyword.other.fake.nexo"], ["returns", "keyword.other.fake.nexo"]]],
    ['summary = ask("Summarise this text", text)', [["ask", "support.function.builtin.nexo"]]],
    ['infra shop:', [["infra", "keyword.other.module.nexo"]]],
    ['pipeline main:', [["pipeline", "keyword.other.module.nexo"]]],
    ['    step build:', [["step", "keyword.other.block.nexo"]]],
    ['    trigger push to main', [["trigger", "keyword.other.block.nexo"]]],
    ['    output url = api.url', [["output", "keyword.other.block.nexo"]]],
    ['        image = build_image("shop-api")', [["build_image", "support.function.builtin.nexo"]]],
  ];
  for (const [line, expected] of cases) {
    const tokens = tokenize(g, line);
    for (const [text, scope] of expected) {
      assert.strictEqual(scopeOf(tokens, text), scope, `${line}: "${text}"`);
    }
  }
  // a multi-line text keeps its interpolations
  const multi = tokenize(g, 'text = """\n    Several lines\n    with {name}\n    """\nx = 1');
  assert.strictEqual(scopeOf(multi, "name"), "source.nexo.embedded");
  assert.strictEqual(scopeOf(multi, "1"), "constant.numeric.nexo");
  console.log(`grammar: ${files.length} programs tokenized, ${cases.length} lines checked`);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
