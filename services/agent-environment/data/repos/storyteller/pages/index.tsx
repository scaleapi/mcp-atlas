import { AspectRatio } from "@/components/ui/aspect-ratio";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { storytellerSchema } from "@/storyteller/storytellerSchema";
import { Loader2, Mic } from "lucide-react";
import { delay } from "modelfusion";
import {
  convertAudioChunksToBase64,
  invokeFlow,
} from "modelfusion-experimental/browser";
import { useRef, useState } from "react";

const baseUrl = process.env.NEXT_PUBLIC_BASE_URL;

export default function Home() {
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const [isRecording, setIsRecording] = useState(false);
  const [waitingForUserInput, setWaitingForUserInput] = useState(true);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [input, setInput] = useState<string | null>(null);
  const [audioUrls, setAudioUrls] = useState<string[]>([]);
  const [activePart, setActivePart] = useState(0);
  const [generatingStory, setGeneratingStory] = useState(false);
  const [shouldAutoPlay, setShouldAutoPlay] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resetError = () => {
    setError(null);
  };

  const startRecording = () => {
    if (isRecording) return;

    resetError(); // Clear any previous errors

    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((stream) => {
        const mediaRecorder = new MediaRecorder(stream);
        mediaRecorderRef.current = mediaRecorder;

        mediaRecorder.ondataavailable = (e) => {
          audioChunksRef.current.push(e.data);
        };

        // .start(1000): workaround for Safari/iphone
        // see https://community.openai.com/t/whisper-api-completely-wrong-for-mp4/289256/12
        mediaRecorder.start(1000);

        setIsRecording(true);
      })
      .catch((error) => {
        setError(
          "Error accessing microphone. Please ensure you have given the necessary permissions."
        );
      });
  };

  const stopRecording = () => {
    const mediaRecorder = mediaRecorderRef.current;

    if (mediaRecorder && isRecording) {
      mediaRecorder.onstop = async () => {
        setWaitingForUserInput(false);
        setGeneratingStory(true);
        setShouldAutoPlay(true);

        try {
          const mimeType = mediaRecorder.mimeType;
          const audioChunks = audioChunksRef.current;

          audioChunksRef.current = [];
          mediaRecorder.stream?.getTracks().forEach((track) => track.stop()); // stop microphone access

          invokeFlow({
            url: `${baseUrl}/generate-story`,
            schema: storytellerSchema,
            input: {
              audioData: await convertAudioChunksToBase64({
                audioChunks,
                mimeType,
              }),
              mimeType,
            },
            onEvent(event) {
              switch (event.type) {
                case "transcribed-input": {
                  setInput(event.input);
                  break;
                }
                case "generated-image": {
                  setImageUrl(event.url);
                  break;
                }
                case "generated-title": {
                  setTitle(event.title);
                  break;
                }
                case "generated-audio-part": {
                  audioUrls[event.index] = event.url;
                  setAudioUrls(audioUrls.slice());
                  break;
                }
              }
            },
            onStop() {
              setGeneratingStory(false);
            },
          });
        } catch (error) {
          console.error("Error generating story:", error);
          setError("An error occurred while generating the story:" + error);
        }
      };

      mediaRecorder.stop();
      setIsRecording(false);
    }
  };

  const onPlaybackEnded = async () => {
    if (activePart === audioUrls.length - 1) {
      setActivePart(0);
      setShouldAutoPlay(false);
    } else {
      await delay(1000); // delay between parts to improve the quality of the story
      setActivePart(activePart + 1);
    }
  };

  return (
    <div className="mx-auto p-5 max-w-[542px]">
      {error && (
        <div
          className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 mb-4 rounded relative"
          role="alert"
        >
          <strong className="font-bold">Error </strong>
          <span className="block sm:inline">{error}</span>
          <span
            className="absolute top-0 bottom-0 right-0 px-4 py-3"
            onClick={resetError}
          >
            <svg
              className="fill-current h-6 w-6 text-red-500"
              role="button"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
            >
              <title>Close</title>
              <path d="M14.348 14.849a1.2 1.2 0 0 1-1.697 0L10 11.819l-2.651 3.029a1.2 1.2 0 1 1-1.697-1.697l2.758-3.15-2.759-3.152a1.2 1.2 0 1 1 1.697-1.697L10 8.183l2.651-3.031a1.2 1.2 0 1 1 1.697 1.697l-2.758 3.152 2.758 3.15a1.2 1.2 0 0 1 0 1.698z" />
            </svg>
          </span>
        </div>
      )}

      {waitingForUserInput ? (
        <Card>
          <CardHeader>
            <CardTitle>Story Teller</CardTitle>
            <CardDescription>
              Automatically generate stories for pre-school kids.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              onClick={isRecording ? stopRecording : startRecording}
              onContextMenu={(e) => e.preventDefault()}
              variant="outline"
            >
              {!isRecording ? (
                <>
                  <Mic className="mr-2 h-4 w-4" />
                  Record topic
                </>
              ) : (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  <i>Recording…</i>
                </>
              )}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            {input ? (
              <CardDescription>&quot;{input}&quot;</CardDescription>
            ) : (
              <Skeleton className="h-4 w-full" />
            )}
            <CardTitle>
              {title ?? <Skeleton className="h-8 w-full" />}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {imageUrl != null ? (
              <div className="w-full">
                <AspectRatio ratio={1 / 1}>
                  <img
                    src={imageUrl}
                    alt={title ?? ""}
                    className="rounded-md object-cover w-full"
                  />
                </AspectRatio>
              </div>
            ) : (
              <Skeleton className="h-96 w-full" />
            )}
          </CardContent>
          <CardFooter className="flex justify-between items-center">
            {audioUrls[activePart] != null ? (
              <>
                <audio
                  controls
                  controlsList="nodownload nofullscreen noremoteplayback"
                  autoPlay={shouldAutoPlay}
                  src={audioUrls[activePart]}
                  onEnded={onPlaybackEnded}
                />
                <span>
                  Part {activePart + 1} /{" "}
                  {generatingStory ? "..." : audioUrls.length}
                </span>
              </>
            ) : (
              <Skeleton className="h-16 w-full" />
            )}
          </CardFooter>
        </Card>
      )}
    </div>
  );
}
