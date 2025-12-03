import cors from "@fastify/cors";
import fastifyStatic from "@fastify/static";
import dotenv from "dotenv";
import Fastify from "fastify";
import { modelfusion } from "modelfusion";
import {
  FileSystemAssetStorage,
  FileSystemLogger,
  modelFusionFastifyPlugin,
} from "modelfusion-experimental/fastify-server";
import path from "node:path";
import { storyTellerFlow } from "./storyTellerFlow";

dotenv.config();

modelfusion.setLogFormat("basic-text");

const port = process.env.PORT ? parseInt(process.env.PORT) : 3001;
const host = process.env.HOST ?? "localhost";
const baseUrl = process.env.BASE_URL ?? `http://${host}:${port}`;
const fsBasePath = process.env.BASE_PATH ?? "runs";

export async function main() {
  try {
    const fastify = Fastify();

    await fastify.register(cors, {});
    await fastify.register(fastifyStatic, {
      root: path.join(__dirname, "..", "..", "out"),
      prefix: "/",
    });

    const logger = new FileSystemLogger({
      path: (run) => path.join(fsBasePath, run.runId, "logs"),
    });

    const assetStorage = new FileSystemAssetStorage({
      path: (run) => path.join(fsBasePath, run.runId, "assets"),
      logger,
    });

    fastify.register(modelFusionFastifyPlugin, {
      baseUrl,
      basePath: "/generate-story",
      flow: storyTellerFlow,
      logger,
      assetStorage,
    });

    console.log(`Starting server on port ${port}...`);
    await fastify.listen({ port, host });
    console.log("Server started");
  } catch (error) {
    console.error("Failed to start server");
    console.error(error);
    process.exit(1);
  }
}

main();
