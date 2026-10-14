"use client";

/**
 * Browser-native speech I/O -- Web Speech API (SpeechRecognition for mic
 * input, SpeechSynthesis for reading answers aloud). No server calls, no
 * API billing, no new dependency. Support varies by browser (best in
 * Chrome/Edge; Firefox has no SpeechRecognition; Safari's is limited) --
 * every entry point here degrades to a no-op when unsupported, callers
 * just hide the button via the isXSupported() checks.
 */

import { useEffect, useRef, useState } from "react";

interface SpeechRecognitionResultLike {
  isFinal: boolean;
  [index: number]: { transcript: string };
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: { length: number; [index: number]: SpeechRecognitionResultLike };
}

interface MinimalSpeechRecognition extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
}

type SpeechRecognitionCtor = new () => MinimalSpeechRecognition;

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function isMicSupported(): boolean {
  return getSpeechRecognitionCtor() !== null;
}

export function isSpeechSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

/**
 * Mic-to-text with live state for a recording UI: `interimText` updates on
 * every partial result (for live-caption display while the user is still
 * speaking), `seconds` ticks once a second for an elapsed-time readout,
 * and `start(onFinal)` calls back only once a phrase is finalized --
 * exactly what should land in a real text input.
 */
export function useMicInput() {
  const [listening, setListening] = useState(false);
  const [interimText, setInterimText] = useState("");
  const [seconds, setSeconds] = useState(0);
  const recognitionRef = useRef<MinimalSpeechRecognition | null>(null);
  const onFinalRef = useRef<((text: string) => void) | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      recognitionRef.current?.abort();
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const start = (onFinal: (text: string) => void) => {
    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) return;
    onFinalRef.current = onFinal;
    const recognition = new Ctor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    recognition.onresult = (event) => {
      let finalText = "";
      let interimText = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interimText += result[0].transcript;
      }
      if (finalText) {
        onFinalRef.current?.(finalText);
        setInterimText("");
      } else {
        setInterimText(interimText);
      }
    };
    const onStopped = () => {
      setListening(false);
      setInterimText("");
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
    recognition.onerror = onStopped;
    recognition.onend = onStopped;
    recognitionRef.current = recognition;
    recognition.start();
    setListening(true);
    setInterimText("");
    setSeconds(0);
    timerRef.current = setInterval(() => setSeconds((s) => s + 1), 1000);
  };

  const stop = () => {
    recognitionRef.current?.stop();
    setListening(false);
    setInterimText("");
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  return { listening, interimText, seconds, start, stop };
}

/** Strips citation markers and markdown punctuation so TTS doesn't read
 * out "bracket bracket one" or literal asterisks. */
export function speakableText(text: string): string {
  return text
    .replace(/\[\[\d+\]\]\(#[^)]*\)/g, "")
    .replace(/[*_#>`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function speak(text: string, onEnd?: () => void): void {
  if (!isSpeechSupported()) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(speakableText(text));
  utterance.onend = () => onEnd?.();
  utterance.onerror = () => onEnd?.();
  window.speechSynthesis.speak(utterance);
}

export function stopSpeaking(): void {
  if (isSpeechSupported()) window.speechSynthesis.cancel();
}
