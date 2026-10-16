/* Local demonstration accounts. Run inside users: node scripts/create-test-users.js */
const mongoose = require("mongoose");
const bcrypt = require("bcrypt");
const User = require("../models/user");
const Users = require("../controllers/user");
const { hashEmail } = require("../utils/fieldCrypto");

const PASSWORD = "Grupo7Teste!2026";
const ACCOUNTS = [
  { name: "Teste Normal 1", email: "normal1@picturas.local", type: "free" },
  { name: "Teste Normal 2", email: "normal2@picturas.local", type: "free" },
  { name: "Teste Premium 1", email: "premium1@picturas.local", type: "premium" },
  { name: "Teste Premium 2", email: "premium2@picturas.local", type: "premium" },
];

async function createTestUsers(accounts = ACCOUNTS, password = PASSWORD) {
  await mongoose.connect("mongodb://users_mongoDB:27019/user", {
    serverSelectionTimeoutMS: 10000,
  });
  try {
    // Check every collision before creating anything. Never reset an existing account.
    const existing = [];
    for (const account of accounts) {
      const user = await User.findOne({ email_hash: hashEmail(account.email) });
      if (user && (user.name !== account.name || user.type !== account.type ||
          !user.password_hash || !await bcrypt.compare(password, user.password_hash))) {
        throw new Error(`A conta ${account.email} já existe com dados diferentes; não foi substituída.`);
      }
      existing.push(user);
    }
    for (let index = 0; index < accounts.length; index++) {
      const account = accounts[index];
      if (existing[index]) {
        console.log(`Já existe: ${account.email} (${account.type}). Dados preservados.`);
      } else {
        await Users.create({ ...account, password, operations: [] });
        console.log(`Criada: ${account.email} (${account.type}).`);
      }
    }
    console.log("Preparação concluída. Credenciais de demonstração no README.md.");
  } finally {
    await mongoose.disconnect();
  }
}

if (require.main === module) {
  createTestUsers().catch((error) => {
    console.error(`Não foi possível preparar as contas: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { ACCOUNTS, PASSWORD, createTestUsers };
