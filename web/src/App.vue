<script setup>
import { RouterView } from 'vue-router'
</script>

<template>
  <div class="app">
    <div class="fx" aria-hidden="true">
      <span class="fx-glow g1"></span>
      <span class="fx-glow g2"></span>
      <span class="fx-veil"></span>
    </div>

    <header class="hud-bar">
      <div class="hud-bar-inner">
        <RouterLink class="brand" to="/" aria-label="Wishlist Monitor">
          <span class="brand-mark">
            <svg viewBox="0 0 40 40" width="30" height="30" aria-hidden="true">
              <g fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 34 C20 24 20 14 20 6" />
                <path d="M20 12 C13 10 8 13 7 20 C15 21 19 18 20 12 Z" fill="currentColor" fill-opacity="0.16" />
                <path d="M20 12 C27 10 32 13 33 20 C25 21 21 18 20 12 Z" fill="currentColor" fill-opacity="0.16" />
                <path d="M20 22 C15 21 11 23 10 29 C17 30 20 27 20 22 Z" fill="currentColor" fill-opacity="0.12" />
                <path d="M20 22 C25 21 29 23 30 29 C23 30 20 27 20 22 Z" fill="currentColor" fill-opacity="0.12" />
                <circle cx="20" cy="6" r="2.2" fill="currentColor" />
              </g>
            </svg>
          </span>
          <span class="brand-text">
            <strong>Wishlist<span class="accent">&nbsp;Monitor</span></strong>
            <small>Steam 愿望单跟踪</small>
          </span>
        </RouterLink>

        <div class="hud-status">
          <span class="hud-tag"><i></i> 实时监测</span>
          <a href="https://store.steampowered.com/app/4759010" target="_blank" class="hud-clock">前往 Steam</a>
        </div>
      </div>
    </header>

    <main class="stage">
      <RouterView />
    </main>

    <footer class="foot">
      <span class="foot-mark" aria-hidden="true"></span>
      <p>Wishlist Monitor · 高玩梁</p>
    </footer>
  </div>
</template>
