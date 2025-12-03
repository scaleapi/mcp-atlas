import {
  MemoryVectorIndex,
  SpeechGenerationModel,
  VectorIndexRetriever,
  ZodSchema,
  elevenlabs,
  generateObject,
  lmnt,
  openai,
  retrieve,
  zodSchema,
} from "modelfusion";
import { readFile } from "node:fs/promises";
import { z } from "zod";

const voiceSchema = z.object({
  provider: z.enum(["lmnt", "elevenlabs"]),
  voiceId: z.string(),
  name: z.string(),
  gender: z.enum(["M", "F"]),
  description: z.string(),
});

export type Voice = z.infer<typeof voiceSchema>;

export class VoiceManager {
  private readonly voiceIndex: MemoryVectorIndex<Voice>;
  private readonly narrator: Voice;
  private readonly speakerToVoice = new Map<string, Voice>();

  static async fromFile({
    voicesPath,
    narrator,
  }: {
    voicesPath: string;
    narrator: Voice;
  }): Promise<VoiceManager> {
    const voicesData = await readFile(voicesPath, "utf8");

    const voiceIndex = await MemoryVectorIndex.deserialize({
      serializedData: voicesData,
      schema: new ZodSchema(voiceSchema),
    });

    return new VoiceManager({ voiceIndex, narrator });
  }

  constructor({
    voiceIndex,
    narrator,
  }: {
    voiceIndex: MemoryVectorIndex<Voice>;
    narrator: Voice;
  }) {
    this.voiceIndex = voiceIndex;
    this.narrator = narrator;
  }

  async getSpeechModel({
    speaker,
    story,
  }: {
    speaker: string;
    story: string;
  }): Promise<SpeechGenerationModel> {
    let voice = this.speakerToVoice.get(speaker);

    if (voice == null) {
      voice = await this.selectVoice({ speaker, story });
      this.speakerToVoice.set(speaker, voice);
    }

    switch (voice.provider) {
      case "lmnt":
        return lmnt.SpeechGenerator({ voice: voice.voiceId });
      case "elevenlabs":
        return elevenlabs.SpeechGenerator({ voice: voice.voiceId });
      default:
        throw new Error(`Unknown voice provider: ${voice.provider}`);
    }
  }

  private async selectVoice({
    speaker,
    story,
  }: {
    speaker: string;
    story: string;
  }): Promise<Voice> {
    // pre-determined narrator voice:
    if (speaker.toLowerCase() === "narrator") {
      return this.narrator;
    }

    // generate voice descriptions for the speakers:
    const voiceDescription = await generateObject({
      functionId: "generate-voice-description",
      model: openai
        .ChatTextGenerator({ model: "gpt-3.5-turbo", temperature: 0 })
        .asFunctionCallObjectGenerationModel({ fnName: "voice" })
        .withTextPrompt(),
      schema: zodSchema(
        z.object({
          gender: z.string().describe("M for male, F for female)"),
          description: z.string().describe("Voice description"),
        })
      ),
      prompt: [
        `## Task`,
        `Generate a voice description for ${speaker} from the following story for an audio book.`,
        "The voice should be appropriate for a preschooler listener.",
        "Include the gender and age in the voice description.",
        "",
        "## Story",
        story,
        "",
        "## Speaker",
        speaker,
        "",
        "## Voice description (incl. age, gender)",
      ].join("\n"),
    });

    // retrieve the voice vectors from the index:
    const potentialVoices = await retrieve(
      new VectorIndexRetriever({
        vectorIndex: this.voiceIndex,
        embeddingModel: openai.TextEmbedder({
          model: "text-embedding-ada-002",
        }),
        maxResults: 5,
        similarityThreshold: 0.2,
        filter: (indexVoice) =>
          indexVoice.provider === "elevenlabs" &&
          (["M", "F"].includes(voiceDescription.gender)
            ? indexVoice.gender === voiceDescription.gender
            : true),
      }),
      voiceDescription.description,
      { functionId: "retrieve-voice" }
    );

    const unavailableVoices = Array.from(this.speakerToVoice.values()).map(
      (voice) => `${voice.provider}:${voice.voiceId}`
    );

    const voice = potentialVoices.find(
      (voice) =>
        !unavailableVoices.includes(`${voice.provider}:${voice.voiceId}`)
    );

    if (!voice) {
      throw new Error(`No voice found for ${speaker}`);
    }

    return voice;
  }
}
