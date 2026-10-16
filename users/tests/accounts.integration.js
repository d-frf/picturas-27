/* Real gateway/database checks; temporary fixtures are removed afterwards. */
const assert = require("node:assert/strict");
const http = require("node:http");
const { randomUUID } = require("node:crypto");
const mongoose = require("mongoose");
const User = require("../models/user");
const Users = require("../controllers/user");
const { hashEmail } = require("../utils/fieldCrypto");
const { ACCOUNTS, PASSWORD, createTestUsers } = require("../scripts/create-test-users");

const fixtures = [];
const fixtureProjects = [];

async function connect() {
  await mongoose.connect("mongodb://users_mongoDB:27019/user", { serverSelectionTimeoutMS: 10000 });
}

function api(method, route, body, token, extraHeaders = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? undefined : JSON.stringify(body);
    const request = http.request({
      hostname: "api_gateway", port: 8000,
      method, path: route,
      headers: { ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}),
        ...(token ? { Authorization: "Bearer " + token } : {}), ...extraHeaders },
    }, (response) => {
      let payload = "";
      response.setEncoding("utf8");
      response.on("data", (part) => { payload += part; });
      response.on("end", () => {
        try { resolve({ status: response.statusCode, body: JSON.parse(payload || "null") }); }
        catch (error) { reject(error); }
      });
    });
    request.setTimeout(15000, () => request.destroy(new Error("Request timed out")));
    request.on("error", reject);
    request.end(data);
  });
}

function publicResponse(value) {
  if (!value || typeof value !== "object") return;
  for (const [key, nested] of Object.entries(value)) {
    assert(!["password", "password_hash", "email_hash", "email_encrypted"].includes(key), `Exposed ${key}`);
    publicResponse(nested);
  }
}

async function login(account, password = PASSWORD) {
  return api("POST", `/users/${encodeURIComponent(account.email)}/login`, { password });
}

async function main() {
  const fixtureEmail = `account-regression-${randomUUID()}@picturas.local`;
  fixtures.push(fixtureEmail);
  try {
    // Register, then log in in a separate request rather than reusing registration's session.
    const registration = await api("POST", "/users/", {
      name: "ACCOUNT REGRESSION FIXTURE", email: fixtureEmail, password: PASSWORD, type: "free",
    });
    assert.equal(registration.status, 201);
    publicResponse(registration.body);
    const registered = await login({ email: fixtureEmail });
    assert.equal(registered.status, 201);
    publicResponse(registered.body);
    assert.equal(registered.body.user._id, registration.body.user._id);

    await connect();
    const ids = ACCOUNTS.map((a) => hashEmail(a.email));
    const before = await User.find({ email_hash: { $in: ids } }).lean();
    await mongoose.disconnect();
    await createTestUsers();
    await connect();
    const after = await User.find({ email_hash: { $in: ids } }).lean();
    assert.equal(after.length, 4);
    assert.equal(JSON.stringify(after), JSON.stringify(before), "Repeated preparation changed existing users");
    await mongoose.disconnect();

    for (const account of ACCOUNTS) {
      const response = await login(account);
      assert.equal(response.status, 201, account.email);
      assert.equal(response.body.user.type, account.type);
      assert.equal(response.body.user.email, account.email);
      publicResponse(response.body);
      const user = response.body.user._id, token = response.body.jwt;
      const details = await api("GET", `/users/${user}`);
      assert.equal(details.status, 200); publicResponse(details.body);
      const session = await api("GET", `/users/validate/${user}`, undefined, token);
      assert.equal(session.status, 200); publicResponse(session.body);
      assert.equal((await login(account, "incorrect-test-password")).status, 401);
      // The four real logins must also authorize the video library on their own projects.
      const project = await api("POST", `/projects/${user}`, { name: "ACCOUNT REGRESSION FIXTURE" }, token);
      assert.equal(project.status, 201);
      fixtureProjects.push({ user, pid: project.body._id, version: project.body.version || 0, token });
      const library = await api("GET", `/projects/${user}/${project.body._id}/videos`, undefined, token);
      assert.equal(library.status, 200); assert.equal(library.body.profile, account.type);
      assert.equal(library.body.can_edit, true);
      console.log(`Login, sessão, respostas públicas e acesso a vídeo: ${account.email} OK`);
    }
    assert.equal((await login({ email: `missing-${randomUUID()}@picturas.local` })).status, 401);
    assert.equal((await api("POST", `/users/${encodeURIComponent(ACCOUNTS[0].email)}/login`, {})).status, 401);

    // A collision must stop preparation without modifying the existing user or creating the other one.
    const conflictEmail = `account-conflict-${randomUUID()}@picturas.local`;
    const unusedEmail = `account-unused-${randomUUID()}@picturas.local`;
    fixtures.push(conflictEmail, unusedEmail);
    await connect();
    await Users.create({ name: "ACCOUNT REGRESSION FIXTURE", email: conflictEmail,
      type: "free", password: PASSWORD, operations: [] });
    const conflictBefore = await User.findOne({ email_hash: hashEmail(conflictEmail) }).lean();
    await mongoose.disconnect();
    await assert.rejects(createTestUsers([
      { name: "Unused", email: unusedEmail, type: "free" },
      { name: "Different name", email: conflictEmail, type: "premium" },
    ]), /não foi substituída/);
    await connect();
    assert.equal(await User.countDocuments({ email_hash: hashEmail(unusedEmail) }), 0);
    assert.equal(JSON.stringify(await User.findOne({ email_hash: hashEmail(conflictEmail) }).lean()), JSON.stringify(conflictBefore));
    console.log("Registo e novo login, recusas, repetição e preservação em conflito: OK");
  } finally {
    const cleanupErrors = [];
    for (const project of fixtureProjects) {
      try {
        const response = await api("DELETE", `/projects/${project.user}/${project.pid}`, undefined,
          project.token, { "X-Project-Version": String(project.version) });
        assert.equal(response.status, 204, "Could not clean fixture project");
      } catch (error) { cleanupErrors.push(error.message); }
    }
    await connect();
    await User.deleteMany({ email_hash: { $in: fixtures.map(hashEmail) } });
    await mongoose.disconnect();
    assert.equal(cleanupErrors.length, 0, cleanupErrors.join("; "));
  }
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
