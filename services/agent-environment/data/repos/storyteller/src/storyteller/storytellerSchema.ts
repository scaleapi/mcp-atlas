import { z } from "zod";

export const storytellerSchema = {
  input: z.object({
    mimeType: z.string(),
    audioData: z.string(),
  }),
  events: z.discriminatedUnion("type", [
    z.object({
      type: z.literal("transcribed-input"),
      input: z.string(),
    }),
    z.object({
      type: z.literal("generated-title"),
      title: z.string(),
    }),
    z.object({
      type: z.literal("generated-image"),
      url: z.string(),
    }),
    z.object({
      type: z.literal("generated-audio-part"),
      index: z.number(),
      url: z.string(),
    }),
  ]),
};
