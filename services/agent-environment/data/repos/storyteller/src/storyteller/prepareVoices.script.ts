import dotenv from "dotenv";
import fs from "fs/promises";
import { MemoryVectorIndex, openai, upsertIntoVectorIndex } from "modelfusion";
import { z } from "zod";
import { Voice } from "./VoiceManager";

dotenv.config();

async function main() {
  try {
    const vectorIndex = new MemoryVectorIndex<Voice>();

    await addLmntVoices(vectorIndex);
    await addElevenLabsVoices(vectorIndex);

    await fs.writeFile("./data/voices.index.json", vectorIndex.serialize());
  } catch (err) {
    console.error("Error reading file", err);
  }
}

const lmntVoiceSchema = z.object({
  id: z.string(),
  name: z.string(),
  gender: z.enum(["M", "F"]),
  tags: z.array(z.string()),
  description: z.string(),
});

type LmntVoice = z.infer<typeof lmntVoiceSchema>;

async function addLmntVoices(vectorIndex: MemoryVectorIndex<Voice>) {
  const data = await fs.readFile("./data/voices.lmnt.json", "utf8");
  const lmntVoices: LmntVoice[] = Object.values(JSON.parse(data).voices);

  const voices: Voice[] = lmntVoices.map((voice) => ({
    voiceId: voice.id,
    name: voice.name,
    provider: "lmnt",
    gender: voice.gender,
    description:
      (voice.gender === "M" ? "Male voice. " : "Female voice. ") +
      voice.tags.join(" ") +
      ". " +
      voice.description,
  }));

  await upsertIntoVectorIndex({
    vectorIndex,
    embeddingModel: openai.TextEmbedder({ model: "text-embedding-ada-002" }),
    objects: voices,
    getValueToEmbed: (voice) => voice.description,
  });
}

const elevenLabsVoiceSchema = z.object({
  voice_id: z.string(),
  name: z.string(),
  labels: z.record(z.string()),
});

type ElevenLabsVoice = z.infer<typeof elevenLabsVoiceSchema>;

async function addElevenLabsVoices(vectorIndex: MemoryVectorIndex<Voice>) {
  const data = await fs.readFile("./data/voices.11labs.json", "utf8");
  const elevenLabsVoices: ElevenLabsVoice[] = Object.values(
    JSON.parse(data).voices
  );

  const voices: Voice[] = elevenLabsVoices
    .filter((voice) => voice.labels.age === "young")
    .map((voice) => ({
      voiceId: voice.voice_id,
      name: voice.name,
      provider: "elevenlabs",
      gender: voice.labels.gender === "female" ? "F" : "M",
      description: Object.entries(voice.labels)
        .map(([key, value]) => `${key}: ${value}`)
        .join(", "),
    }));

  await upsertIntoVectorIndex({
    vectorIndex,
    embeddingModel: openai.TextEmbedder({ model: "text-embedding-ada-002" }),
    objects: voices,
    getValueToEmbed: (voice) => voice.description,
  });
}

main();
