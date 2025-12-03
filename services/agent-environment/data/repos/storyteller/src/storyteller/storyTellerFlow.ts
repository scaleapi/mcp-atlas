import {
  generateImage,
  generateSpeech,
  generateText,
  generateTranscription,
  openai,
  stability,
  streamObject,
  zodSchema,
} from "modelfusion";
import { DefaultFlow } from "modelfusion-experimental/fastify-server";
import { z } from "zod";
import { VoiceManager } from "./VoiceManager";
import { storytellerSchema } from "./storytellerSchema";

export const storyTellerFlow = new DefaultFlow({
  schema: storytellerSchema,
  async process({ input: { mimeType, audioData }, run }) {
    // Transcribe the user voice input:
    const transcription = await generateTranscription({
      functionId: "transcribe",
      model: openai.Transcriber({ model: "whisper-1" }),
      mimeType,
      audioData,
    });

    run.publishEvent({ type: "transcribed-input", input: transcription });

    // Generate a story based on the transcription:
    const story = await generateText({
      functionId: "generate-story",
      model: openai.CompletionTextGenerator({
        model: "gpt-3.5-turbo-instruct",
        temperature: 1.2,
        maxGenerationTokens: 1000,
      }),
      prompt:
        "Generate a story aimed at preschoolers on the following topic: \n" +
        `'${transcription}'.`,
    });

    // Run in parallel:
    await Promise.allSettled([
      // Generate title:
      (async () => {
        const title = await generateText({
          functionId: "generate-title",
          model: openai.CompletionTextGenerator({
            model: "gpt-3.5-turbo-instruct",
            temperature: 0.7,
            maxGenerationTokens: 200,
            stopSequences: ['"'],
          }),
          prompt:
            "Generate a short title for the following story for pre-school children: \n\n" +
            `'${story}'.\n\n` +
            'Title: "',
        });

        run.publishEvent({ type: "generated-title", title });
      })(),

      // Generate image that represents story:
      (async () => {
        const imagePrompt = await generateText({
          functionId: "generate-story-image-prompt",
          model: openai
            .ChatTextGenerator({
              model: "gpt-4",
              temperature: 0,
              maxGenerationTokens: 500,
            })
            .withTextPrompt(),
          prompt:
            "Generate a short image generation prompt " +
            "(only abstract keywords, max 8 keywords) for the following story: " +
            story,
        });

        const storyImage = await generateImage({
          functionId: "generate-story-image",
          model: stability
            .ImageGenerator({
              model: "stable-diffusion-xl-1024-v1-0",
              cfgScale: 7,
              height: 1024,
              width: 1024,
              steps: 30,
            })
            .withTextPrompt(),
          prompt: `${imagePrompt} style of colorful illustration for a preschooler story`,
        });

        const imagePath = await run.storeBinaryAsset({
          name: "story.png",
          data: Buffer.from(storyImage),
          contentType: "image/png",
        });

        run.publishEvent({ type: "generated-image", url: imagePath });
      })(),

      // expand and narrate story:
      (async () => {
        const voiceManager = await VoiceManager.fromFile({
          voicesPath: "./data/voices.index.json",
          narrator: {
            voiceId: "c8ea4f2a-06e6-4d7b-9484-db941bf7c657",
            name: "Joe",
            provider: "lmnt",
            gender: "M",
            description: "Male voice. Middle-aged.",
          },
        });

        const narratedStoryPartSchema = z.object({
          type: z
            .enum(["narration", "dialogue"])
            .describe("Type of story part. Either 'narration' or 'dialogue'."),
          speaker: z
            .string()
            .describe(
              "Speaker of a dialogue (direct speech) part. Must be a single speaker."
            ),
          content: z.string().describe("Content of the story part"),
        });

        type NarratedStoryPart = z.infer<typeof narratedStoryPartSchema>;

        const structuredStorySchema = z.object({
          parts: z.array(narratedStoryPartSchema),
        });

        const processedParts: Array<NarratedStoryPart> = [];

        const { objectStream: audioStoryStream, objectPromise } =
          await streamObject({
            functionId: "generate-audio-story",
            model: openai
              .ChatTextGenerator({
                model: "gpt-4",
                temperature: 0,
              })
              .asFunctionCallObjectGenerationModel({
                fnName: "story",
                fnDescription: "Kids story with narration.",
              })
              .withTextPrompt(),
            schema: zodSchema(structuredStorySchema),
            prompt: [
              "Expand the following story into a longer, narrated audio story for preschoolers.",
              "",
              "The audio story should include interesting dialogue by the main characters.",
              "The language should be understandable by a preschooler.",
              "",
              "Add details and dialog to make the story parts longer.",
              "Add the speaker to each dialogue part. A dialogue part can only have one speaker.",
              "There must only be one narrator.",
              "Each spoken part must be a dialogue part with a speaker.",
              "",
              "Story:",
              story,
            ].join("\n"),
            fullResponse: true,
          });

        for await (const { partialObject } of audioStoryStream) {
          if (partialObject.parts == null) {
            continue;
          }

          // the last story part might not be complete yet:
          const partialParts = partialObject.parts.slice(0, -1);

          // ensure that the remaining story parts are complete:
          const partialPartsParseResult = z
            .array(narratedStoryPartSchema)
            .safeParse(partialParts);

          if (partialPartsParseResult.success) {
            await processNewParts(partialPartsParseResult.data);
          }
        }

        // process the remaining parts:
        const audioStory = await objectPromise;
        await processNewParts(audioStory.parts);

        async function processNewParts(parts: NarratedStoryPart[]) {
          const newParts = parts.slice(processedParts.length);
          processedParts.push(...newParts);

          for (const part of newParts) {
            const index = processedParts.indexOf(part);
            const speaker = part.speaker;

            const narrationAudio = await generateSpeech({
              functionId: "narrate-story-part",
              model: await voiceManager.getSpeechModel({ speaker, story }),
              text: part.content,
            });

            const path = await run.storeBinaryAsset({
              name: `story-part-${index}.mp3`,
              data: Buffer.from(narrationAudio),
              contentType: "audio/mpeg",
            });

            run.publishEvent({
              type: "generated-audio-part",
              index,
              url: path,
            });
          }
        }
      })(),
    ]);
  },
});
