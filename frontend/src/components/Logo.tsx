"use client";

import Image from "next/image";
import { motion, useReducedMotion } from "framer-motion";

interface Props {
  className?: string;
  compact?: boolean;
  inverse?: boolean;
  showDescriptor?: boolean;
}

/** ALAIN wordmark from public/alain-logo.svg */
export function Logo({ className = "", compact = false }: Props) {
  const reduced = useReducedMotion();
  const height = compact ? 22 : 32;

  return (
    <motion.span
      initial={reduced ? false : { opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: "easeOut" }}
      className={`inline-flex select-none items-center overflow-hidden rounded-sm ${className}`}
      aria-label="ALAIN"
    >
      <Image
        src="/alain-logo.svg"
        alt="ALAIN"
        width={compact ? 100 : 140}
        height={height}
        className="h-auto w-auto object-contain object-left"
        style={{ height, width: "auto" }}
        priority
        unoptimized
      />
    </motion.span>
  );
}
