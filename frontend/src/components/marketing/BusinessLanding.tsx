"use client";

import { motion, useReducedMotion } from "framer-motion";
import Image from "next/image";
import { ArrowRight, BookOpen, Check } from "lucide-react";
import { Logo } from "@/components/Logo";

interface Props {
  onSignIn: () => void;
}

const ease = [0.22, 1, 0.36, 1] as const;

export function BusinessLanding({ onSignIn }: Props) {
  const reduced = useReducedMotion();

  return (
    <main className="min-h-screen overflow-hidden bg-black text-white">
      <header className="fixed inset-x-0 top-0 z-50 border-b border-white/20 bg-black/70 backdrop-blur-md">
        <div className="mx-auto flex h-[76px] max-w-[1480px] items-center px-5 sm:px-10 lg:px-16">
          <a href="#top" aria-label="ALAIN home">
            <Logo inverse showDescriptor />
          </a>
          <div className="ml-auto flex items-center gap-3">
            <a
              href="/RAG.html"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-10 items-center gap-2 border border-white/30 px-5 text-xs font-medium text-white/85 transition duration-500 hover:border-white hover:text-white"
            >
              <BookOpen className="h-3.5 w-3.5" /> Know more
            </a>
            <button
              type="button"
              onClick={onSignIn}
              className="inline-flex h-10 items-center gap-2 border border-white px-5 text-xs font-medium text-white transition duration-500 hover:bg-white hover:text-black"
            >
              Open workspace <ArrowRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </header>

      <section
        id="top"
        className="relative flex min-h-screen items-center overflow-hidden bg-black px-5 py-32 sm:px-8 sm:py-40"
      >
        <Image
          src="/images/rag-pipeline-hero-4k.webp"
          alt="Documents transformed into semantic chunks, retrieved evidence, and a cited answer"
          fill
          priority
          sizes="100vw"
          className="object-cover object-center"
        />
        <div className="pointer-events-none absolute inset-0 bg-black/20" />
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_center,rgba(0,0,0,.88)_0%,rgba(0,0,0,.78)_34%,rgba(0,0,0,.5)_62%,rgba(0,0,0,.42)_100%)]" />
        <div className="pointer-events-none absolute inset-y-0 left-0 right-0 bg-[linear-gradient(180deg,rgba(0,0,0,.72)_0%,transparent_28%,transparent_72%,rgba(0,0,0,.72)_100%)]" />

        <div className="relative mx-auto w-full max-w-[1480px]">
          <div className="mx-auto max-w-4xl text-center">
            <motion.div
              initial={reduced ? false : { opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.55, ease }}
              className="hairline-label !text-white/55"
            >
              Document intelligence for high-stakes work
            </motion.div>
            <motion.h1
              initial={reduced ? false : { opacity: 0, y: 22 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, delay: 0.08, ease }}
              className="font-display text-balance mt-8 text-[clamp(3.6rem,8vw,8.4rem)] font-medium leading-[0.88] tracking-[-0.045em]"
            >
              RAG
            </motion.h1>
            <motion.p
              initial={reduced ? false : { opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.65, delay: 0.16, ease }}
              className="text-balance mx-auto mt-8 max-w-2xl text-base font-light leading-7 text-white/62 sm:text-lg"
            >
              RAG turns complex operational documents into decision-ready answers, with the page,
              source, and visual evidence attached.
            </motion.p>
            <motion.div
              initial={reduced ? false : { opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.65, delay: 0.24, ease }}
              className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row"
            >
              <button
                type="button"
                onClick={onSignIn}
                className="inline-flex h-12 items-center gap-2 border border-white bg-white px-7 text-sm font-medium text-black transition duration-500 hover:bg-transparent hover:text-white"
              >
                Try it out <ArrowRight className="h-4 w-4" />
              </button>
            </motion.div>
            <motion.div
              initial={reduced ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.7, delay: 0.42 }}
              className="mt-9 flex flex-wrap items-center justify-center gap-x-7 gap-y-2 text-[10px] font-medium uppercase tracking-[0.08em] text-white/50"
            >
              {["Page-level citations", "Visual document analysis", "Multi-format ingestion"].map(
                (item) => (
                  <span key={item} className="inline-flex items-center gap-1.5">
                    <Check className="h-3.5 w-3.5 text-white" />
                    {item}
                  </span>
                ),
              )}
            </motion.div>
          </div>
        </div>
      </section>
    </main>
  );
}
