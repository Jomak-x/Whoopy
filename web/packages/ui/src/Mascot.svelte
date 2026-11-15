<script lang="ts">
  type Pose = 'logo' | 'seated' | 'sleeping' | 'concerned';
  let { pose = 'logo', alt = '', className = '', asset }: { pose?: Pose; alt?: string; className?: string; asset?: string } = $props();
  const assets: Record<Pose, string> = {
    logo: '/cat-logo-v5-head-128.png',
    seated: '/cat-seated-v3-tight.png',
    sleeping: '/cat-sleeping-v3-tight.png',
    concerned: '/cat-concerned-v4-tight.png'
  };
</script>

<img class={className} src={asset || assets[pose]} {alt} />

<style>
  img { display: block; object-fit: contain; }
</style>
