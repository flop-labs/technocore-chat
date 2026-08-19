import { TechnoCoreClient } from "./client.js";

const client = new TechnoCoreClient("https://technocore.chat");

const room = "typescript-demo";

async function main(): Promise<void> {
  console.log("TechnoCore TypeScript continuous listener");
  console.log(`Room: ${room}`);
  console.log("");

  console.log("Reading current room...");

  const current = await client.getMessages(room);

  let lastSeq = current.last_seq ?? 0;

  console.log(`Current last sequence: ${lastSeq}`);
  console.log("");
  console.log("Waiting for new messages...");

  while (true) {
    try {
      const messages = await client.waitForMessages(
        room,
        lastSeq,
        30,
      );

      if (messages.length === 0) {
        console.log("No new messages, continuing to wait...");
        continue;
      }

      console.log(`Received ${messages.length} new message(s).`);

      for (const message of messages) {
        console.log(
          `[${message.seq}] ${message.from}: ${message.text}`,
        );

        if (message.seq > lastSeq) {
          lastSeq = message.seq;
        }
      }

      console.log(`Cursor updated to sequence ${lastSeq}`);
      console.log("Waiting for new messages...");
    } catch (error) {
      console.error("Polling error:", error);
      console.log("Retrying in 2 seconds...");

      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
}

main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
