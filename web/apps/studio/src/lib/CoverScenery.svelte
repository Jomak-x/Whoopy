<script lang="ts">
  import type { CoverTheme } from './libraryPresentation';
  export let scene: CoverTheme;
  export let foreground = false;
</script>

<!-- Deliberately drawn in layers: the seat is behind the cat, while only a
     shallow lip or distant corner detail sits in front of its contact point. -->
<g stroke-linecap="round" stroke-linejoin="round">
  {#if foreground}
    {#if scene === 'fireplace'}
      <path d="M124 152 Q211 160 299 152 L292 168 Q208 176 132 167Z" fill="#b97d49" stroke="#795235" stroke-width="1.2"/>
      <path d="M131 157 Q211 166 293 158 M133 163 Q211 172 291 164" fill="none" stroke="#e7ba79" stroke-width="1.6"/>
      {#each [141,154,167,180,193,206,219,232,245,258,271,284] as x}<path d={`M${x} 158 l-1 10`} stroke="#845737" stroke-width=".9"/>{/each}
      <path d="M247 154 Q261 157 280 155 L278 177 246 174Z" fill="#8cabb8" stroke="#496d85" stroke-width="1"/>
      <path d="M252 158 251 172 M259 159 258 173 M266 158 265 173 M273 158 272 174" stroke="#f5dec2" stroke-width="2"/>
    {:else if scene === 'reading'}
      <path d="M115 146 Q197 153 289 146 L286 155 Q202 161 118 154Z" fill="#c87043" stroke="#935133" stroke-width="1.1"/>
      <path d="M125 150 Q202 156 280 150" fill="none" stroke="#efaa6d" stroke-width="1"/>
    {:else if scene === 'night'}
      <path d="M89 143 H292 V150 H89Z" fill="#c48b63" stroke="#7a5c4b" stroke-width="1.1"/>
      <path d="M95 145 H285" stroke="#edc99a" stroke-width="1.3"/>
      <path d="M104 150 V174 H126 V150" fill="#bc755b"/><path d="M109 152 V170 M116 152 V172 M123 152 V170" stroke="#e6ad86" stroke-width="1"/>
    {:else if scene === 'sunroom'}
      <path d="M38 145 Q136 151 235 145 L232 153 H41Z" fill="#d9854b" stroke="#ad673f" stroke-width="1.1"/>
      <path d="M48 148 H226" stroke="#f4bd77" stroke-width="1.1"/>
    {:else if scene === 'garden'}
      <path d="M85 147 H285 V153 H85Z" fill="#dbb979" stroke="#94734b" stroke-width="1.1"/>
      <path d="M8 180 Q12 162 5 149 M11 172 Q24 160 25 153 M309 180 Q301 162 312 149" fill="none" stroke="#50735c" stroke-width="2"/>
      <ellipse cx="10" cy="160" rx="7" ry="3.5" fill="#8ba573" transform="rotate(55 10 160)"/><ellipse cx="22" cy="161" rx="7" ry="3.5" fill="#91a878" transform="rotate(-45 22 161)"/>
    {:else if scene === 'cafe'}
      <path d="M83 146 Q181 151 282 146 L280 154 H85Z" fill="#b76e4b" stroke="#80583d" stroke-width="1.1"/>
      <path d="M90 149 H275" stroke="#e7ac78" stroke-width="1"/>
    {:else if scene === 'shore'}
      <path d="M286 180 299 139 M299 162 311 150 M296 164 290 153 M306 180 315 161" stroke="#8c9364" stroke-width="1.3" fill="none"/>
      <path d="M10 178 q9-14 18-2 q-10 5-18 2Z" fill="#ecd0a5" stroke="#b99671" stroke-width=".8"/><path d="M15 176 22 173" stroke="#b99671" stroke-width=".8"/>
    {:else if scene === 'village'}
      <path d="M95 149 H292 V156 H95Z" fill="#bd784e" stroke="#744c36" stroke-width="1.2"/>
      <path d="M241 151 H280 L277 178 H237Z" fill="#b15a4e" stroke="#743b3c" stroke-width="1"/>
      <path d="M244 155 H277 M242 163 H277 M240 171 H276 M248 153 245 175 M262 153 259 176 M273 153 270 176" stroke="#eec79c" stroke-width="1.2"/>
    {:else if scene === 'mountain'}
      <path d="M4 180 9 156 M7 170 1 162 M10 169 17 158 M277 180 286 157 M283 169 296 161" fill="none" stroke="#5c8274" stroke-width="1.5"/>
      <path d="M255 178 262 171 275 172 281 180" fill="#819a95" stroke="#617f7c" stroke-width="1"/>
    {:else if scene === 'dunes'}
      <path d="M92 156 294 157 M98 160 289 161" stroke="#dfab78" stroke-width="1.1"/>
      {#each [103,111,119,127,135,143,151,159,167,175,183,191,199,207,215,223,231,239,247,255,263,271,279,287] as x}<path d={`M${x} 161 l-1 3`} stroke="#e5c290" stroke-width=".8"/>{/each}
    {/if}
  {:else if scene === 'night'}
    <rect width="320" height="180" fill="#23354f"/>
    <path d="M0 160 H320 V180 H0Z" fill="#684b4a"/><path d="M0 173 H320 M42 160 35 180 M292 160 303 180" stroke="#463e46" stroke-width="1"/>
    <path d="M94 15 Q191-3 288 15 V140 H94Z" fill="#bd8b60" stroke="#1c293e" stroke-width="2"/>
    <path d="M102 21 Q190 5 280 21 V135 H102Z" fill="#355779" stroke="#efc490" stroke-width="1.2"/>
    <path d="M103 103 128 86 162 108 196 83 230 101 253 82 279 99 V135 H103Z" fill="#28465f"/>
    <path d="M103 117 Q155 103 205 119 T279 111 V135 H103Z" fill="#213c53"/>
    <circle cx="246" cy="47" r="15" fill="#f5d991"/><circle cx="252" cy="42" r="14" fill="#355779"/>
    <g fill="#f2deb2"><circle cx="120" cy="37" r="1.1"/><circle cx="151" cy="53" r="1.3"/><circle cx="182" cy="28" r="1"/><circle cx="215" cy="69" r="1"/><circle cx="266" cy="83" r="1"/></g>
    <path d="M189 13 V136 M103 83 H280" stroke="#c69569" stroke-width="4"/><path d="M191 17 V132 M106 81 H276" stroke="#f0c894" stroke-width="1"/>
    <path d="M78 0 H116 Q103 65 110 124 L78 133Z M274 0 H311 L315 135 278 124 Q290 65 274 0Z" fill="#536783" stroke="#23364e" stroke-width="1.2"/>
    <path d="M88 7 Q91 65 87 125 M100 4 Q98 64 101 122 M288 4 Q301 67 287 124 M300 7 305 129" fill="none" stroke="#8191a6" stroke-width="1.1"/>
    <path d="M81 111 106 109 M281 109 310 115" stroke="#d6af77" stroke-width="3"/>
    <path d="M89 139 Q102 133 125 136 H269 Q285 134 292 140 V144 H89Z" fill="#d09b77" stroke="#885f4c" stroke-width="1"/>
    <path d="M110 137 Q144 131 174 137 M237 137 Q263 132 282 138" fill="none" stroke="#efbc93" stroke-width="1"/>
    <path d="M18 146 H71 V153 H18Z M26 153 V180 M64 153 V180" fill="#a47452" stroke="#3a3040" stroke-width="1.4"/>
    <path d="M43 138 V86 M32 140 H55" stroke="#c29761" stroke-width="3"/>
    <path d="M28 57 H57 L66 87 H19Z" fill="#f4ca7f" stroke="#b18554" stroke-width="1.2"/><path d="M34 60 31 82 M50 60 55 82" stroke="#ffdf9c" stroke-width="1"/>
    <path d="M24 143 H62 V147 H24Z" fill="#9e6956" stroke="#543c41" stroke-width=".8"/><path d="M27 142 H59" stroke="#e6cda2" stroke-width="2"/>
    <rect x="22" y="16" width="36" height="30" rx="1" fill="#a9815b" stroke="#192b43" stroke-width="1.4"/><rect x="26" y="20" width="28" height="22" fill="#e5c69d"/><path d="M30 36 39 27 49 36" stroke="#66818b" stroke-width="1.4" fill="none"/>
    <path d="M92 152 H290 V159 H92Z" fill="#957151" stroke="#4c3e42" stroke-width="1"/>
  {:else if scene === 'reading'}
    <rect width="320" height="180" fill="#f3d9a4"/>
    <path d="M0 160 H320 V180 H0Z" fill="#b57d57"/><path d="M0 174 H320 M32 161 26 180 M310 160 317 180" stroke="#936044" stroke-width="1"/>
    <rect x="9" y="13" width="88" height="145" rx="2" fill="#a76d48" stroke="#644833" stroke-width="1.3"/><rect x="15" y="19" width="76" height="132" fill="#5b625b"/>
    {#each [54,96,138] as y}
      <path d={`M14 ${y} H92 v5 H14Z`} fill="#d39459" stroke="#734d36" stroke-width=".9"/>
      <path d={`M23 ${y-28} V${y-2}`} stroke="#ce7759" stroke-width="9"/><path d={`M35 ${y-32} V${y-2}`} stroke="#e7bf7a" stroke-width="8"/><path d={`M47 ${y-26} V${y-2}`} stroke="#7f9b9b" stroke-width="9"/><path d={`M60 ${y-30} V${y-2}`} stroke="#b75743" stroke-width="8"/><path d={`M73 ${y-29} l7 27`} stroke="#f1d5a0" stroke-width="8"/>
      <path d={`M20 ${y-21} H26 M32 ${y-24} H38 M44 ${y-19} H50 M57 ${y-23} H63 M73 ${y-22} l5-1 M20 ${y-6} H26 M32 ${y-7} H38 M44 ${y-6} H50`} stroke="#f7e3bc" stroke-width="1"/>
    {/each}
    <rect x="125" y="13" width="44" height="35" fill="#b77a4e" stroke="#835c3e" stroke-width="1"/><rect x="130" y="18" width="34" height="25" fill="#f7edcf"/><path d="M135 37 145 24 159 37" fill="#9eae91"/><circle cx="156" cy="25" r="3" fill="#e1ad60"/>
    <path d="M254 17 H303 V109 H254Z" fill="#b88c5e" stroke="#855f44" stroke-width="1"/><path d="M260 23 H297 V102 H260Z" fill="#b6cfd0"/><path d="M260 82 Q276 65 297 79 V102 H260Z" fill="#829e8e"/><path d="M278 23 V104 M260 63 H297" stroke="#f2dfb8" stroke-width="3"/>
    <path d="M250 109 H307 V114 H250Z" fill="#c59c6a" stroke="#956a47" stroke-width=".8"/>
    <path d="M119 141 V76 Q119 61 138 61 H271 Q287 61 287 78 V145Z" fill="#dc8e51" stroke="#965831" stroke-width="1.5"/>
    <path d="M129 131 V79 Q129 70 140 70 H269 Q278 70 278 80 V131" fill="none" stroke="#f2b571" stroke-width="1.3"/>
    <path d="M128 85 Q145 87 159 84 M245 85 Q262 88 277 84" fill="none" stroke="#c77d45" stroke-width="1"/>
    <path d="M113 139 Q156 132 202 137 Q247 132 291 139 V148 H113Z" fill="#ecab66" stroke="#a86537" stroke-width="1.3"/>
    <path d="M116 146 V124 Q105 116 108 109 Q111 103 119 108 L133 137 M288 145 V123 Q300 116 297 109 Q294 104 286 109 L276 137" fill="#bd7043" stroke="#875039" stroke-width="1.3"/>
    <path d="M126 155 121 175 M277 155 284 175" stroke="#684a35" stroke-width="5"/>
    <path d="M105 177 H302" stroke="#e2ad75" stroke-width="1.6"/>
    <path d="M188 4 H220 L229 23 H178Z" fill="#f8edc7" stroke="#b88c57" stroke-width="1.1"/><path d="M204 0 V4" stroke="#9c734a" stroke-width="1.5"/>
    <path d="M24 159 Q43 155 60 160 Q75 155 90 160 V170 Q75 165 60 171 Q43 166 24 170Z" fill="#f6e6ba" stroke="#97673e" stroke-width="1"/><path d="M60 160 V170 M30 161 Q42 159 54 162 M67 162 Q78 159 85 161 M30 165 Q43 162 54 166" fill="none" stroke="#be9d6e" stroke-width=".8"/>
  {:else if scene === 'dawn'}
    <rect width="320" height="180" fill="#f5c6a6"/>
    <path d="M0 57 Q79 37 150 53 T320 36 V89 H0Z" fill="#efad92"/>
    <circle cx="250" cy="68" r="25" fill="#ffe3a0"/><path d="M210 72 H290 M221 78 H277" stroke="#f5c6a6" stroke-width="2"/>
    <path d="M0 111 48 79 97 99 151 70 207 109 259 89 320 106 V146 H0Z" fill="#bc8290"/><path d="M0 128 Q75 100 139 121 T259 117 T320 122 V154 H0Z" fill="#936e83"/>
    <path d="M0 143 Q75 112 160 139 T320 129 V180 H0Z" fill="#b56f59"/>
    <path d="M0 154 H320 V180 H0Z" fill="#d99a6e"/><path d="M0 169 H320 M47 154 31 180 M252 154 275 180" stroke="#b77d59" stroke-width="1"/>
    <path d="M223 131 H320 V138 H223Z M231 138 V158 M266 138 V158 M301 138 V158" fill="#e7b489" stroke="#9e6956" stroke-width="1.1"/>
    <path d="M17 145 Q112 138 207 145 V154 H17Z" fill="#f2cf9f" stroke="#a96d50" stroke-width="1.3"/><path d="M24 154 H201 V171 H24Z" fill="#c18a68" stroke="#a96d50" stroke-width="1.1"/>
    <path d="M32 159 H82 M96 159 H146 M159 159 H194 M63 154 V171 M150 154 V171" stroke="#a97455" stroke-width=".8"/>
    <path d="M28 146 H194" stroke="#ffe3b4" stroke-width="1.5"/>
    <path d="M253 164 H283 L278 177 H258Z" fill="#a25d49" stroke="#7a5047" stroke-width="1.2"/><path d="M257 164 Q244 150 251 139 Q261 134 267 146 Q270 129 279 135 Q288 146 279 164" fill="#6f8976" stroke="#536b60" stroke-width="1.1"/>
    <path d="M267 163 V145 M268 154 277 144 M266 153 256 145" stroke="#b0b98c" stroke-width=".9"/>
    <path d="M189 35 q6-4 12 0 q6-4 12 0 M285 45 q4-3 8 0 q4-3 8 0" fill="none" stroke="#ad7b7d" stroke-width="1.3"/>
    <path d="M14 177 H51 M218 177 H242" stroke="#efbd8d" stroke-width="1.1"/>
  {:else if scene === 'garden'}
    <rect width="320" height="180" fill="#e6edda"/>
    <path d="M0 83 Q20 45 47 74 Q81 36 113 75 Q143 51 168 76 Q209 43 238 74 Q281 40 320 75 V133 H0Z" fill="#a4ba8d"/>
    <path d="M0 109 Q32 85 68 105 T136 99 T210 105 T285 99 T320 102 V145 H0Z" fill="#799a76"/>
    <path d="M0 125 H320 V153 H0Z" fill="#c9c5a3"/><path d="M0 140 H320 M27 125 V140 M78 140 V153 M134 125 V140 M238 125 V140 M286 140 V153" stroke="#a8ac89" stroke-width="1"/>
    <path d="M0 154 Q87 143 167 156 T320 152 V180 H0Z" fill="#d9c89e"/><path d="M76 180 102 155 H249 L285 180" fill="#e9dcb8"/>
    <path d="M15 143 V29 Q43 16 70 29 V143 M15 51 H70 M15 80 H70 M15 109 H70 M33 25 V144 M52 25 V143" fill="none" stroke="#b38b59" stroke-width="2"/>
    <path d="M23 143 Q8 109 31 83 Q59 67 56 29 M62 144 Q82 116 57 91" fill="none" stroke="#5f8261" stroke-width="2"/>
    {#each [[25,115,-35],[29,88,35],[46,76,-30],[56,48,30],[65,119,35],[58,95,-30]] as [x,y,r]}
      <ellipse cx={x} cy={y} rx="9" ry="4.5" transform={`rotate(${r} ${x} ${y})`} fill="#7f9d71" stroke="#567b5b" stroke-width=".7"/>
    {/each}
    <g fill="#cf98a3" stroke="#ac7587" stroke-width=".7"><circle cx="26" cy="85" r="5"/><circle cx="59" cy="50" r="5"/><circle cx="61" cy="116" r="5"/></g>
    <path d="M92 141 V97 Q92 88 102 88 H273 Q281 88 281 97 V143 M96 104 H278 M96 118 H278 M111 91 V137 M263 91 V137" fill="none" stroke="#a88753" stroke-width="4"/>
    <path d="M96 97 H278 M96 111 H278 M96 126 H278" stroke="#d7b675" stroke-width="6"/>
    <path d="M85 143 H285 V148 H85Z" fill="#e9cd8c" stroke="#94734b" stroke-width="1.2"/>
    <path d="M100 151 95 175 M271 151 278 175" stroke="#806b4a" stroke-width="4"/>
    <path d="M290 153 V123 M302 154 V115 M312 154 V127" stroke="#647f5a" stroke-width="1.5"/><g fill="#9c82b6" stroke="#7b6595" stroke-width=".7"><ellipse cx="290" cy="123" rx="3" ry="9"/><ellipse cx="302" cy="115" rx="3.5" ry="11"/><ellipse cx="312" cy="127" rx="3" ry="8"/></g>
    <path d="M35 166 H57 M222 172 H244" stroke="#b8a981" stroke-width="1"/><path d="M253 28 q3-5 5 0 q5-2 3 3 q-4 3-8-3Z" fill="#dbb56f"/>
  {:else if scene === 'mountain'}
    <rect width="320" height="180" fill="#c8e5ed"/>
    <path d="M0 105 59 35 109 93 171 18 247 102 286 56 320 94 V139 H0Z" fill="#87abc4" stroke="#749ab5" stroke-width=".8"/>
    <path d="m37 61 22-26 24 28-14-7-10 5-8-7Z m108-10 26-33 34 38-21-9-12 9-13-12Z m123 28 18-23 18 23-12-7-7 5-7-7Z" fill="#f7f5e8"/>
    <path d="M0 126 28 91 76 125 129 70 186 127 235 84 280 127 320 109 V149 H0Z" fill="#527f9b"/>
    <path d="m129 70-6 33 12-7 8 24 M235 84l-4 28 10-6" fill="none" stroke="#709ab0" stroke-width="1.3"/>
    <path d="M0 126 Q88 121 157 129 H320 V169 H0Z" fill="#75b3c1"/><path d="M153 135 H225 M191 142 H285 M243 131 H305 M223 155 H296" stroke="#b4d7d7" stroke-width="1.5"/>
    <path d="M0 155 Q40 135 93 148 Q133 144 169 151 L199 180 H0Z" fill="#849d86"/>
    <path d="M25 145 40 138 99 139 146 135 191 140 207 146 192 163 158 172 71 169 30 160Z" fill="#bbc0ad" stroke="#7f9284" stroke-width="1.3"/>
    <path d="M29 147 80 151 143 147 201 148 M81 151 74 167 M145 149 158 171 M35 155 60 159 M167 160 185 157" stroke="#8f9e8e" stroke-width="1.1" fill="none"/>
    <path d="M40 143 93 144 140 140 185 144" stroke="#e0dcc3" stroke-width="1.2" fill="none"/>
    <path d="M297 144 V77 M278 112l19-35 19 35Z M274 130l23-39 23 39Z M273 146l24-36 23 36Z" fill="#467b76" stroke="#386563" stroke-width="1"/>
    <path d="M17 137 V101 m-12 22 12-22 12 22Z m-15-13-14 27 H32Z" fill="#628d7c"/>
    <path d="M22 24 Q36 18 49 24 M233 29 Q250 24 269 29" fill="none" stroke="#f4f8ee" stroke-width="3"/>
    <path d="M213 174 222 170 231 174 M9 171 H23" fill="none" stroke="#d3cba9" stroke-width="1.5"/>
  {:else if scene === 'shore'}
    <rect width="320" height="180" fill="#f7dfb4"/>
    <circle cx="64" cy="48" r="20" fill="#fff0c2"/>
    <path d="M0 78 H320 V143 H0Z" fill="#77bac5"/><path d="M0 96 Q57 89 121 98 T252 96 T320 94 V145 H0Z" fill="#4f9eaf"/>
    <path d="M0 117 Q67 96 126 118 T258 117 T320 109 V180 H0Z" fill="#e7c38f"/><path d="M0 115 Q67 95 126 116 T258 115 T320 107" fill="none" stroke="#f8f3d9" stroke-width="3"/>
    <path d="M0 127 Q61 112 117 130 T238 132" fill="none" stroke="#f5ddb5" stroke-width="1.2"/>
    <path d="M0 143 320 137 V180 H0Z" fill="#b88c5e" stroke="#896b4b" stroke-width="1.1"/>
    <path d="M0 156 320 150 M0 171 320 165 M48 143 35 180 M122 141 118 180 M228 139 238 180 M290 138 306 180" stroke="#8b6d4e" stroke-width="1"/>
    <path d="M7 148 40 147 M127 159 163 158 M247 173 284 172" stroke="#dcb583" stroke-width="1"/>
    <path d="M96 141 286 142 294 159 88 158Z" fill="#f4e7c5" stroke="#cfb68e" stroke-width=".9"/>
    <path d="M113 142 108 158 M128 142 124 158 M258 142 263 159 M273 142 279 159" stroke="#508ca1" stroke-width="5"/>
    <path d="M91 160 88 165 M99 160 97 165 M108 160 106 165 M283 161 285 166 M292 161 295 166" stroke="#e5cda6" stroke-width="1.1"/>
    <path d="M14 147 V102 M53 145 V99 M14 107 53 104" stroke="#a57a50" stroke-width="5"/><path d="M14 111 Q33 121 53 108" fill="none" stroke="#e7cd9c" stroke-width="2"/>
    <path d="M21 87 H58 M141 86 H179 M263 81 H295" stroke="#b6dad6" stroke-width="1.6"/>
    <path d="M233 37 q6-4 12 0 q6-4 12 0 M267 46 q4-3 8 0 q4-3 8 0" fill="none" stroke="#739598" stroke-width="1.3"/>
    <path d="M30 135 q5-7 10 0 q-5 4-10 0Z" fill="#e7b6a0" stroke="#b88c74" stroke-width=".8"/>
  {:else if scene === 'sunroom'}
    <rect width="320" height="180" fill="#f7dfae"/>
    <rect x="25" y="11" width="226" height="128" fill="#c79255" stroke="#a27749" stroke-width="1.2"/><rect x="32" y="18" width="212" height="115" fill="#c4dcd1"/>
    <path d="M33 99 Q73 65 109 90 Q157 56 195 88 Q220 73 244 88 V133 H33Z" fill="#9eb78e"/><path d="M33 120 Q83 94 132 116 T244 111 V133 H33Z" fill="#7fa183"/>
    <path d="M102 18 V133 M174 18 V133 M33 75 H244" stroke="#fff0cc" stroke-width="5"/>
    <path d="M5 155 H320 V180 H5Z" fill="#cc9661"/><path d="M0 172 H320 M74 154 66 180 M267 155 282 180" stroke="#ad784c" stroke-width="1"/>
    <path d="M38 134 H236 V145 H38Z" fill="#eeb96b" stroke="#af7846" stroke-width="1.2"/><path d="M39 138 Q76 132 136 138 Q197 132 234 138" fill="none" stroke="#ffe0a1" stroke-width="1.2"/>
    <path d="M42 152 H230 V170 H42Z" fill="#bc8b51" stroke="#8d693e" stroke-width="1.1"/><path d="M49 155 H224 M52 160 H221 M56 165 H218" stroke="#e1b776" stroke-width="1.2"/>
    <path d="M62 153 V169 M77 153 V170 M92 153 V169 M107 153 V170 M122 153 V170 M137 153 V170 M152 153 V170 M167 153 V170 M182 153 V170 M197 153 V170 M212 153 V170" stroke="#9d753f" stroke-width=".8"/>
    <path d="M251 0 H294 L287 116 256 128 Q267 67 251 0Z" fill="#f2bf79" stroke="#cf9557" stroke-width="1"/><path d="M264 4 Q279 66 265 120 M277 7 278 116" stroke="#ffdd9b" stroke-width="1.2" fill="none"/>
    <path d="M269 155 V87 M271 120 291 105 M268 113 254 99" fill="none" stroke="#66826a" stroke-width="2"/>
    <g fill="#7e9b6e" stroke="#577d5c" stroke-width=".9"><ellipse cx="291" cy="103" rx="7" ry="13" transform="rotate(40 291 103)"/><ellipse cx="253" cy="96" rx="7" ry="13" transform="rotate(-40 253 96)"/><ellipse cx="269" cy="82" rx="7" ry="14"/></g>
    <path d="M253 148 H289 L284 173 H259Z" fill="#cb7650" stroke="#9e563e" stroke-width="1.2"/><path d="M251 147 H291 V153 H251Z" fill="#e59b68" stroke="#a35f41" stroke-width="1"/><path d="M260 158 H283 M261 165 H282" stroke="#eeb383" stroke-width="1"/>
    <path d="M5 40 H24 M12 34 V46" stroke="#fdf1c9" stroke-width="1"/><path d="M8 155 33 143 33 180 H8Z" fill="#f9cf84" opacity=".6"/>
  {:else if scene === 'balcony'}
    <rect width="320" height="180" fill="#edc5c2"/>
    <path d="M0 88 Q88 72 162 84 T320 70 V120 H0Z" fill="#dbafb8"/>
    <path d="M0 127 V89 H33 V73 H63 V110 H105 V91 H146 V120 H194 V86 H228 V107 H261 V82 H304 V110 H320 V151 H0Z" fill="#9d91ad"/>
    <path d="M31 74 48 59 65 74 M191 86 211 71 231 86 M259 83 282 65 305 83" fill="#7f7896"/>
    <g fill="#f1d6a4"><path d="M41 83 H47 V94 H41Z M55 101 H61 V112 H55Z M202 94 H208 V104 H202Z M279 92 H285 V103 H279Z M293 114 H299 V124 H293Z"/></g>
    <path d="M0 148 H320 V180 H0Z" fill="#cf997c"/><path d="M0 168 H320 M32 150 12 180 M156 150 153 180 M283 150 305 180" stroke="#b6816d" stroke-width="1"/>
    <path d="M0 103 H320 M0 137 H320 M9 104 V144 M48 104 V144 M87 104 V144 M126 104 V144 M165 104 V144 M204 104 V144 M243 104 V144 M282 104 V144 M317 104 V144" fill="none" stroke="#666b83" stroke-width="2.5"/>
    <path d="m20 121 9-12 9 12-9 12Z m234 0 9-12 9 12-9 12Z m273 0 9-12 9 12-9 12Z" fill="none" stroke="#8a7f93" stroke-width="1"/>
    <path d="M20 140 Q119 133 220 140 V147 H20Z" fill="#e1ae87" stroke="#9e6e57" stroke-width="1.2"/><path d="M24 147 H217 V159 H24Z" fill="#b58169" stroke="#926653" stroke-width="1"/><path d="M31 160 V176 M209 160 V176" stroke="#765447" stroke-width="4"/>
    <path d="M29 141 Q49 136 70 141 M170 140 Q192 136 211 141" fill="none" stroke="#f5d1a4" stroke-width="1"/>
    <path d="M244 141 H286 L281 166 H249Z" fill="#a96b58" stroke="#7a544e" stroke-width="1.1"/><path d="M240 138 H290 V145 H240Z" fill="#c5896c" stroke="#845e51" stroke-width="1"/>
    <path d="M250 138 V111 M265 138 V103 M279 138 V113" stroke="#688270" stroke-width="1.6"/><path d="M251 132 243 121 M265 124 274 115 M279 133 287 124" stroke="#82a17e" stroke-width="3"/>
    <g fill="#dc947d" stroke="#b77775" stroke-width=".8"><circle cx="250" cy="109" r="5"/><circle cx="265" cy="101" r="5.5"/><circle cx="279" cy="111" r="5"/></g>
    <path d="M0 9 Q140 41 320 9" fill="none" stroke="#826c7e" stroke-width="1"/><g fill="#ffe4a1" stroke="#c79668" stroke-width=".7"><circle cx="20" cy="14" r="3"/><circle cx="76" cy="23" r="3"/><circle cx="139" cy="26" r="3"/><circle cx="204" cy="25" r="3"/><circle cx="267" cy="17" r="3"/><circle cx="307" cy="12" r="3"/></g>
  {:else if scene === 'fireplace'}
    <rect width="320" height="180" fill="#334960"/>
    <path d="M0 157 H320 V180 H0Z" fill="#865944"/><path d="M0 171 H320 M37 157 29 180 M304 157 313 180" stroke="#634835" stroke-width="1"/>
    <path d="M0 145 H320 V157 H0Z" fill="#263d52"/>
    <path d="M18 48 H109 V145 H18Z" fill="#bc7956" stroke="#80503c" stroke-width="1.2"/>
    <path d="M18 69 H109 M18 89 H109 M18 109 H109 M18 130 H109 M43 49 V69 M84 49 V69 M64 70 V89 M38 90 V109 M88 90 V109 M63 110 V130 M40 130 V145 M87 130 V145" stroke="#8f5842" stroke-width="1"/>
    <path d="M34 141 V93 Q34 77 63 77 Q94 77 94 94 V141Z" fill="#3c3540" stroke="#d99566" stroke-width="2"/>
    <path d="M45 136 Q31 119 49 105 Q45 120 60 116 Q59 96 74 90 Q67 110 83 120 Q95 133 82 141Z" fill="#e58e48"/><path d="M52 138 Q45 123 61 119 Q58 131 70 129 Q81 133 73 141Z" fill="#ffd482"/>
    <path d="m42 140 43-5 m-39-2 35 10" stroke="#76523d" stroke-width="4"/><path d="M11 45 H116 V54 H11Z M9 145 H119 V155 H9Z" fill="#d4a373" stroke="#8f6447" stroke-width="1.2"/>
    <path d="M18 149 H111" stroke="#f2c791" stroke-width="1"/>
    <path d="M29 43 V30 H48 V43Z" fill="#d1b388" stroke="#856749" stroke-width="1"/><path d="M34 33 H43 V40 H34Z" fill="#567480"/>
    <path d="M86 44 V32 M80 44 H93" stroke="#cda161" stroke-width="2"/><path d="M84 32 V23 H89 V32Z" fill="#eed09a"/><path d="M85 21 Q82 17 87 14 Q90 19 87 21" fill="#efb566"/>
    <rect x="147" y="16" width="39" height="36" fill="#bd8b55" stroke="#23374e" stroke-width="1.3"/><rect x="152" y="21" width="29" height="26" fill="#e7d2a4"/><path d="M156 42 165 29 177 42" fill="#7d9d90"/><circle cx="174" cy="28" r="3" fill="#d7a568"/>
    <path d="M278 141 V77 M266 144 H291" stroke="#cca775" stroke-width="2.8"/>
    <path d="M263 43 H291 L303 79 H251Z" fill="#f5ce87" stroke="#a98055" stroke-width="1.2"/><path d="M269 47 266 75 M285 47 290 75" stroke="#ffe5ab" stroke-width="1.1"/>
    <path d="M123 150 Q121 133 142 134 H280 Q300 133 302 151Z" fill="#bb8a55" stroke="#7b5539" stroke-width="1.3"/>
    <path d="M126 148 Q140 137 159 141 Q190 136 213 141 Q245 136 275 140 Q291 140 299 149 Q213 158 126 148Z" fill="#d6b08b" stroke="#aa8263" stroke-width="1"/>
    <path d="M133 147 Q146 141 161 145 M258 145 Q279 142 294 149" stroke="#efd0aa" stroke-width="1.1" fill="none"/>
    <path d="M18 164 H77 V174 H18Z" fill="#aa7954" stroke="#6e503b" stroke-width="1"/><path d="M25 166 37 172 M37 166 49 172 M49 166 61 172 M61 166 73 172" stroke="#d09d6a" stroke-width="1"/>
  {:else if scene === 'rooftops'}
    <rect width="320" height="180" fill="#2d3d63"/>
    <path d="M0 81 Q153 62 320 75 V131 H0Z" fill="#615876"/>
    <circle cx="259" cy="51" r="13" fill="#f5dca1"/>
    <g fill="#e7d9bd"><circle cx="32" cy="32" r="1"/><circle cx="83" cy="18" r="1.2"/><circle cx="151" cy="37" r="1"/><circle cx="204" cy="22" r="1"/><circle cx="295" cy="58" r="1"/></g>
    <path d="M0 139 V104 H29 V86 H65 V120 H101 V95 H139 V129 H172 V82 H209 V107 H240 V71 H279 V110 H310 V92 H320 V163 H0Z" fill="#686281" stroke="#454c6c" stroke-width=".8"/>
    <path d="M26 87 47 72 68 87 M169 83 191 65 212 83 M237 72 260 52 282 72 M306 93 319 82 332 93" fill="#3e496b"/>
    <path d="M252 57 V44 H259 V58 M181 72 V56 H188 V68" fill="#52607a"/>
    <g fill="#efbd77"><path d="M37 96 H43 V108 H37Z M52 111 H58 V122 H52Z M111 104 H117 V116 H111Z M126 121 H132 V133 H126Z M181 93 H188 V105 H181Z M196 111 H202 V123 H196Z M250 82 H257 V94 H250Z M265 101 H272 V114 H265Z M250 127 H257 V139 H250Z M291 122 H298 V135 H291Z"/></g>
    <path d="M217 149 H320 V180 H217Z" fill="#5d5e75"/><path d="M224 161 H320 M253 149 242 180 M292 150 306 180" stroke="#434e66" stroke-width="1"/>
    <path d="M0 143 H215 V153 H0Z" fill="#c6ac9f" stroke="#7e7180" stroke-width="1.2"/><path d="M0 153 H211 V180 H0Z" fill="#9f8d91" stroke="#746b7d" stroke-width="1.1"/>
    <path d="M0 168 H211 M35 154 V168 M100 154 V168 M165 154 V168 M69 169 V180 M139 169 V180" stroke="#7e7380" stroke-width=".9"/>
    <path d="M22 144 H203 V149 H22Z" fill="#7596ad" stroke="#526f8a" stroke-width="1"/>
    <path d="M27 146 H200" stroke="#d2c8b6" stroke-width="1"/><path d="M27 153 H62 V180 H27Z" fill="#7596ad"/><path d="M32 154 V180 M42 154 V180 M52 154 V180 M60 154 V180" stroke="#e5cbae" stroke-width="2"/>
    <path d="M0 7 Q142 55 320 18" fill="none" stroke="#172f4d" stroke-width="1.2"/><g fill="#f4ce86" stroke="#b79b72" stroke-width=".7"><circle cx="18" cy="13" r="2.8"/><circle cx="65" cy="25" r="2.8"/><circle cx="117" cy="32" r="2.8"/><circle cx="170" cy="34" r="2.8"/><circle cx="225" cy="30" r="2.8"/><circle cx="278" cy="23" r="2.8"/></g>
    <path d="M280 169 H309 V175 H280Z M284 175 V180 M305 175 V180" fill="#997352" stroke="#584c46" stroke-width="1"/>
  {:else if scene === 'dunes'}
    <rect width="320" height="180" fill="#323d65"/>
    <circle cx="244" cy="37" r="14" fill="#f3d8a1"/><circle cx="250" cy="32" r="13" fill="#323d65"/>
    <g fill="#e9dfca"><circle cx="26" cy="30" r="1.2"/><circle cx="67" cy="57" r="1"/><circle cx="112" cy="26" r="1.3"/><circle cx="155" cy="47" r="1"/><circle cx="195" cy="20" r="1"/><circle cx="281" cy="67" r="1.2"/><circle cx="303" cy="23" r="1"/></g>
    <path d="M26 30 67 57 112 26 M67 57 83 77" stroke="#7782a4" stroke-width=".65" fill="none"/>
    <path d="M0 127 Q78 61 158 112 Q231 148 320 85 V180 H0Z" fill="#977f9b"/>
    <path d="M0 141 Q70 102 148 137 Q231 163 320 111 V180 H0Z" fill="#b5909b"/>
    <path d="M0 158 Q76 132 153 146 Q235 160 320 134 V180 H0Z" fill="#d7b18e"/>
    <path d="M14 132 Q63 101 106 116 M239 145 Q279 134 309 119" fill="none" stroke="#e1b79e" stroke-width="1.2"/>
    <path d="M0 171 Q85 148 174 162 T320 154" fill="none" stroke="#e9c69e" stroke-width="1.5"/>
    <path d="M104 143 285 145 295 161 92 159Z" fill="#9b726f" stroke="#775a65" stroke-width="1.1"/>
    <path d="M109 145 105 158 M119 145 115 159 M273 146 279 160 M283 146 289 160" stroke="#d7ab7e" stroke-width="2"/>
    <path d="M109 149 H281 M105 155 H285" stroke="#ba8b77" stroke-width="1"/>
    <path d="M38 158 H61 V129 H38Z" fill="#c39a65" stroke="#695463" stroke-width="1.2"/><path d="M42 134 H57 V154 H42Z" fill="#f9db91"/><path d="M37 129 H62 L57 124 H43Z M36 158 H63" fill="#665969" stroke="#534b61" stroke-width="1.1"/><path d="M44 124 V119 Q50 112 56 119 V124" fill="none" stroke="#baa083" stroke-width="1.3"/>
    <path d="M300 175 305 154 M303 167 296 160 M305 165 312 155 M15 176 17 161 M17 172 23 165" stroke="#8f8a73" stroke-width="1.2"/>
    <path d="M66 173 q8-3 15 0 M36 176 q6-3 11-1" fill="none" stroke="#b48d79" stroke-width="1.1"/>
  {:else if scene === 'harbor'}
    <rect width="320" height="180" fill="#d5e3e7"/>
    <path d="M0 88 41 72 76 84 121 69 164 88 216 76 272 86 320 72 V109 H0Z" fill="#98b0bd"/>
    <path d="M0 103 H320 V161 H0Z" fill="#639eae"/><path d="M0 119 Q61 110 123 119 T248 117 T320 117 M15 139 H69 M254 136 H309" fill="none" stroke="#a8cccf" stroke-width="1.4"/>
    <path d="M13 102 H93 L81 114 H29Z" fill="#f1e1bd" stroke="#657e88" stroke-width="1"/><path d="M53 35 V102" stroke="#667987" stroke-width="1.5"/><path d="M49 40 21 95 H49Z" fill="#f4e9ce" stroke="#b6b9af" stroke-width=".8"/><path d="M57 51 83 94 H57Z" fill="#d6bf9b" stroke="#a5a99e" stroke-width=".8"/>
    <path d="M247 106 H309 L298 115 H258Z" fill="#3c647e" stroke="#2e5269" stroke-width="1"/><path d="M258 87 H288 L296 106 H252Z" fill="#e5c8a3" stroke="#7d9a9e" stroke-width=".9"/><path d="M263 91 H283 V101 H263Z" fill="#709eab"/>
    <path d="M0 154 320 144 V180 H0Z" fill="#b8895d" stroke="#8e6a4c" stroke-width="1.1"/><path d="M0 168 320 157 M39 152 25 180 M88 151 78 180 M247 146 264 180 M294 145 317 180" stroke="#8e6a4c" stroke-width="1"/>
    <path d="M91 142 V125 H281 V143 M98 130 H274" fill="none" stroke="#789298" stroke-width="4"/>
    <path d="M86 143 H284 V150 H86Z" fill="#e6dab9" stroke="#9d937c" stroke-width="1.2"/><path d="M94 150 V173 M276 150 V169" stroke="#687b7c" stroke-width="4"/>
    <path d="M97 141 H274 V146 H97Z" fill="#96b3bb" stroke="#6d909f" stroke-width=".9"/><path d="M110 142 V146 M124 142 V146 M251 142 V146 M265 142 V146" stroke="#e9dec1" stroke-width="4"/>
    <path d="M21 154 V138 H35 V154 M19 137 H38" fill="#466979" stroke="#385769" stroke-width="3"/><path d="M27 141 Q48 140 54 154 Q63 171 40 172 Q16 171 14 161 Q13 151 34 151 Q54 153 50 164 Q45 171 30 165 Q23 161 34 158" fill="none" stroke="#dfc69b" stroke-width="2"/>
    <path d="M190 37 Q207 31 225 37 M271 28 H293" stroke="#f5f0df" stroke-width="2.5" fill="none"/>
    <path d="M19 70 q4-3 8 0 q4-3 8 0" stroke="#839da6" stroke-width="1.1" fill="none"/>
  {:else if scene === 'canal'}
    <rect width="320" height="180" fill="#f6dbb3"/>
    <path d="M0 111 V56 H38 V111 M38 111 V37 H76 V111 M76 111 V67 H107 V111 M204 111 V54 H243 V111 M243 111 V30 H283 V111 M283 111 V61 H320 V111" fill="#d59876" stroke="#aa7866" stroke-width=".8"/>
    <path d="M40 40 H74 V111 H40Z M245 34 H281 V111 H245Z" fill="#e2b988"/>
    <path d="M0 56 19 39 38 56 M38 37 57 20 76 37 M204 54 224 36 243 54 M243 30 263 12 283 30 M283 61 302 45 321 61" fill="#657b8b" stroke="#586a7b" stroke-width="1"/>
    <path d="M6 60 H33 M43 43 H70 M210 59 H238 M249 38 H277" stroke="#f2d8aa" stroke-width="2"/>
    <g fill="#f8e7c3" stroke="#a17b65" stroke-width=".7"><path d="M7 69 H16 V83 H7Z M23 69 H32 V83 H23Z M46 52 H54 V66 H46Z M61 52 H69 V66 H61Z M46 79 H54 V93 H46Z M61 79 H69 V93 H61Z M85 79 H95 V94 H85Z M212 69 H221 V83 H212Z M227 69 H236 V83 H227Z M251 48 H260 V62 H251Z M268 48 H277 V62 H268Z M251 77 H260 V92 H251Z M268 77 H277 V92 H268Z M293 73 H302 V87 H293Z M307 73 H315 V87 H307Z"/></g>
    <path d="M0 111 H320 V160 H0Z" fill="#72a7aa"/><path d="M0 110 H107 M204 110 H320" stroke="#b7a18a" stroke-width="4"/><path d="M224 126 H272 M241 139 H304 M265 149 H312 M12 125 H45" stroke="#b1d0c1" stroke-width="1.4"/>
    <path d="M1 141 Q107 132 212 140 V153 H0Z" fill="#e3c59c" stroke="#ab8b69" stroke-width="1.2"/><path d="M0 153 H210 V180 H0Z" fill="#bf9b7c"/>
    <path d="M0 167 H210 M29 153 V166 M96 153 V166 M165 153 V166 M62 167 V180 M133 167 V180" stroke="#a68166" stroke-width="1"/><path d="M16 144 Q107 137 202 143" stroke="#f6dfb8" stroke-width="1.4" fill="none"/>
    <path d="M225 163 H320 V180 H225Z" fill="#b59d82"/><path d="M222 158 H320" stroke="#dfcaaa" stroke-width="4"/>
    <g fill="none" stroke="#546e7b" stroke-width="1.4"><circle cx="254" cy="153" r="9"/><circle cx="290" cy="153" r="9"/><path d="m254 153 12-19 13 19H254l11-14h17l8 14 M278 133 l4-6h6 M263 134h9"/></g>
    <path d="M10 140 V119 H40 V139" fill="#b97b5b" stroke="#915e48" stroke-width="1"/><path d="M15 119 Q13 105 23 107 Q26 95 32 107 Q44 105 39 119" fill="#7d9a76" stroke="#647f62" stroke-width=".8"/>
    <path d="M130 34 q5-3 10 0 q5-3 10 0 M175 49 q4-3 8 0 q4-3 8 0" stroke="#ae9c8c" stroke-width="1.2" fill="none"/>
  {:else if scene === 'cafe'}
    <rect width="320" height="180" fill="#d8b58b"/>
    <rect x="68" y="9" width="235" height="130" fill="#786d5c" stroke="#655648" stroke-width="1.2"/><rect x="75" y="16" width="221" height="117" fill="#adc9ce"/>
    <path d="M75 111 V79 H105 V91 H126 V62 H162 V90 H197 V71 H229 V96 H261 V69 H296 V133 H75Z" fill="#83a5af"/><path d="M75 121 Q146 112 211 121 T296 116 V133 H75Z" fill="#668b9b"/>
    <path d="M143 16 V133 M219 16 V133" stroke="#c19a6c" stroke-width="5"/><path d="M145 18 V129 M221 18 V129" stroke="#efd3a5" stroke-width="1"/>
    {#each [[89,29],[112,47],[130,23],[159,39],[183,25],[205,59],[236,34],[264,22],[284,59],[92,84],[245,101],[280,111]] as [x,y]}
      <path d={`M${x} ${y} l-3 9`} stroke="#e4e6da" stroke-width="1.3"/>
    {/each}
    <path d="M67 135 H306 V142 H67Z" fill="#c79869" stroke="#916c4d" stroke-width="1.2"/>
    <path d="M0 158 H320 V180 H0Z" fill="#a77552"/><path d="M0 173 H320 M53 159 45 180 M289 159 301 180" stroke="#815c43" stroke-width="1"/>
    <path d="M83 140 V118 Q83 109 94 109 H272 Q283 109 283 119 V145" fill="#bf7c55" stroke="#895a42" stroke-width="1.2"/>
    <path d="M91 119 H275 M106 115 V136 M257 115 V136" stroke="#dca575" stroke-width="1.1"/>
    <path d="M84 140 Q129 133 182 138 Q230 132 280 139 V147 H84Z" fill="#dba06b" stroke="#a16c47" stroke-width="1.2"/>
    <path d="M90 154 H276 V174 H90Z" fill="#916549" stroke="#6f513c" stroke-width="1"/><path d="M97 158 H269 M103 163 H263 M108 168 H258" stroke="#b18357" stroke-width="1"/>
    <path d="M26 0 V30 M13 30 Q26 18 41 30 L47 39 H7Z" fill="#b47743" stroke="#805a3e" stroke-width="1.3"/><path d="M13 40 H40" stroke="#ffe2a0" stroke-width="2"/>
    <path d="M4 119 H63 V126 H4Z M34 126 V169 M18 172 H51" fill="#a5774e" stroke="#795839" stroke-width="2"/>
    <path d="M19 110 H40 V117 Q28 123 19 117Z" fill="#f5e2b7" stroke="#b59672" stroke-width="1"/><path d="M40 112 Q51 108 47 116 Q45 119 40 117" fill="none" stroke="#f5e2b7" stroke-width="2.5"/><path d="M24 103 Q20 96 26 91 M33 103 Q29 98 33 94" fill="none" stroke="#f0dfc0" stroke-width="1"/>
    <rect x="15" y="59" width="35" height="28" fill="#9b764f" stroke="#7d5a3f" stroke-width="1"/><rect x="19" y="63" width="27" height="20" fill="#e6d5af"/><path d="M24 77 30 68 40 77" fill="#91a58d"/>
  {:else if scene === 'lighthouse'}
    <rect width="320" height="180" fill="#d4e6df"/>
    <path d="M104 82 H320 V155 H104Z" fill="#7ab6bf"/><path d="M129 96 H181 M226 91 H287 M191 110 H245 M274 121 H313" stroke="#b8d8d3" stroke-width="1.5"/>
    <path d="M0 119 33 103 75 109 110 130 148 144 167 180 H0Z" fill="#8da88b"/><path d="M0 141 Q72 119 128 148 L149 180 H0Z" fill="#b8b88d"/>
    <path d="M0 161 Q89 141 157 150 T320 144 V180 H0Z" fill="#dfcda1"/><path d="M0 173 Q78 152 157 162 T320 155" fill="none" stroke="#f0dfb6" stroke-width="1.6"/>
    <path d="M36 111 44 40 H67 L75 111Z" fill="#f6ecd0" stroke="#a3a895" stroke-width="1.1"/><path d="M41 65 H70 L72 84 H39Z" fill="#c67e64"/><path d="M44 40 V27 H68 V40 M39 41 H73 M38 26 56 14 74 26Z" fill="#596f78" stroke="#4d6570" stroke-width="1.2"/><path d="M48 29 H64 V38 H48Z" fill="#f4cf87"/><path d="M52 97 H61 V111 H52Z" fill="#668389"/><path d="M29 112 H83" stroke="#667e74" stroke-width="3"/>
    <path d="M101 145 V111 M289 145 V111 M101 116 H289 M101 130 H289" fill="none" stroke="#8c9a8a" stroke-width="4"/>
    <path d="M105 117 H285 M105 130 H285" stroke="#d3cfad" stroke-width="6"/><path d="M111 116 H139 M247 131 H278" stroke="#a9b59d" stroke-width="1"/>
    <path d="M96 145 H294 V152 H96Z" fill="#e2d8b8" stroke="#9d9e85" stroke-width="1.2"/><path d="M108 152 102 174 M282 152 289 174" stroke="#809083" stroke-width="4"/>
    <path d="M23 164 27 142 M27 154 36 144 M26 155 18 148 M305 172 310 150" fill="none" stroke="#839269" stroke-width="1.3"/>
    <path d="M137 34 Q153 27 171 34 M234 25 Q253 19 274 25" fill="none" stroke="#f4f3dd" stroke-width="3"/><path d="M216 56 q5-3 10 0 q5-3 10 0" stroke="#7f9ca0" stroke-width="1.2" fill="none"/>
    <path d="M51 172 58 168 69 171" fill="none" stroke="#bcaa85" stroke-width="1.2"/>
  {:else if scene === 'village'}
    <rect width="320" height="180" fill="#708caa"/>
    <path d="M0 112 41 59 88 101 139 40 199 107 257 63 320 114 V158 H0Z" fill="#a8bccb"/><path d="m25 79 16-20 22 25-15-7-8 5Z m92-13 22-26 25 31-15-7-10 7-9-13Z" fill="#eaf0e9"/>
    <path d="M0 130 Q67 114 130 133 T260 125 T320 128 V180 H0Z" fill="#d9e4e7"/>
    <path d="M18 134 V94 H76 V137Z" fill="#ad7d5e" stroke="#806952" stroke-width="1.1"/><path d="m9 95 38-31 39 31Z" fill="#485e74" stroke="#3f5267" stroke-width="1"/><path d="m12 91 35-27 36 27-17-6-19-14-20 15Z" fill="#f1f2e8"/>
    <path d="M59 75 V55 H66 V81" fill="#8e96a0"/><path d="M62 50 Q55 42 63 37" fill="none" stroke="#bac8ce" stroke-width="2.5"/>
    <path d="M27 105 H40 V120 H27Z M53 105 H67 V120 H53Z" fill="#f1c783" stroke="#775d47" stroke-width="1"/><path d="M33 106 V119 M60 106 V119" stroke="#d59b64" stroke-width="1"/>
    <path d="M42 118 H51 V137 H42Z" fill="#6f655c"/><path d="M6 139 Q45 130 86 140" fill="none" stroke="#f2f4ec" stroke-width="4"/>
    <g fill="#edf1e9"><circle cx="25" cy="24" r="1.2"/><circle cx="81" cy="41" r="1.4"/><circle cx="112" cy="20" r="1.2"/><circle cx="231" cy="33" r="1.2"/><circle cx="267" cy="54" r="1.2"/><circle cx="169" cy="29" r="1.4"/></g>
    <path d="M84 0 H320 V13 H84Z M84 0 H97 V180 H84Z M304 0 H320 V180 H304Z" fill="#7c563e" stroke="#5d4637" stroke-width="1"/>
    <path d="M90 16 V171 M312 16 V171 M104 5 H297" stroke="#a77b50" stroke-width="1"/>
    <path d="M97 158 H304 V180 H97Z" fill="#a77750"/><path d="M97 172 H304 M119 158 114 180 M276 158 286 180" stroke="#805d43" stroke-width="1"/>
    <path d="M100 142 V113 H288 V145 M103 118 H284 M103 130 H284" fill="none" stroke="#b48354" stroke-width="5"/>
    <path d="M96 141 H289 V149 H96Z" fill="#e1b77e" stroke="#956b45" stroke-width="1.2"/><path d="M102 143 H283" stroke="#f3d5a3" stroke-width="1"/>
    <path d="M109 157 V176 M279 157 V176" stroke="#76523b" stroke-width="4"/>
    <path d="M266 15 V30 M257 31 H276 V57 H257Z" fill="#d3a568" stroke="#6d543f" stroke-width="1.2"/><path d="M261 35 H272 V53 H261Z" fill="#f8db95"/><path d="M255 31 266 23 278 31 M255 57 H278" fill="#654f40" stroke="#654f40" stroke-width="1.2"/>
    <path d="M4 162 Q26 154 56 160 M18 177 Q41 167 71 172" fill="none" stroke="#f3f3e9" stroke-width="3"/>
  {:else if scene === 'observatory'}
    <rect width="320" height="180" fill="#263458"/>
    <path d="M0 125 42 103 93 121 155 86 211 120 270 97 320 117 V155 H0Z" fill="#465878"/><path d="M0 146 Q67 121 135 141 T260 131 T320 138 V180 H0Z" fill="#6c7187"/>
    <g fill="#ede0bd"><circle cx="24" cy="26" r="1.2"/><circle cx="79" cy="17" r="1"/><circle cx="111" cy="41" r="1.5"/><circle cx="159" cy="24" r="1"/><circle cx="185" cy="65" r="1"/><circle cx="222" cy="30" r="1.3"/><circle cx="269" cy="19" r="1"/><circle cx="301" cy="52" r="1.3"/></g>
    <path d="M79 17 111 41 159 24 M111 41 123 61 M222 30 269 19 301 52" fill="none" stroke="#8292af" stroke-width=".7"/>
    <path d="M279 76 H287 M283 72 V80" stroke="#d6d0bc" stroke-width="1"/>
    <path d="M12 115 V88 H74 V115Z" fill="#9caabb" stroke="#74849c" stroke-width="1"/><path d="M12 88 A31 31 0 0 1 74 88Z" fill="#c6d0cf" stroke="#8798ac" stroke-width="1"/><path d="M40 57 Q53 68 52 88 M10 88 H76 M9 115 H77" fill="none" stroke="#768ca7" stroke-width="2"/>
    <path d="m47 69 14-10 5 7-14 11Z" fill="#7288a7" stroke="#536b8b" stroke-width=".8"/><path d="m61 58 6 9" stroke="#dddcc9" stroke-width="2"/>
    <path d="M28 100 H37 V114 H28Z" fill="#6b7d96"/><path d="M56 96 H65 V105 H56Z" fill="#e8c991"/>
    <path d="M0 158 H320 V180 H0Z" fill="#92909d"/><path d="M0 173 H320 M45 158 35 180 M276 158 290 180" stroke="#73798d" stroke-width="1"/>
    <path d="M25 142 H222 V151 H25Z" fill="#c4bdba" stroke="#8f8d9c" stroke-width="1.2"/><path d="M32 151 H215 V165 H32Z" fill="#a39da6" stroke="#838392" stroke-width="1"/><path d="M43 166 V178 M203 166 V178" stroke="#646a81" stroke-width="4"/>
    <path d="M33 145 H214" stroke="#e1d3bb" stroke-width="1.1"/>
    <path d="M241 169 265 116 290 169 M265 116 V170 M251 146 H278" fill="none" stroke="#bbbab8" stroke-width="2.3"/>
    <path d="m246 113 37-17 5 11-37 17Z" fill="#e3d7b8" stroke="#9a9eab" stroke-width="1.2"/><path d="m281 94 7 16" stroke="#8299b2" stroke-width="4"/><path d="m249 114 3 7" stroke="#b39a78" stroke-width="1"/>
    <path d="M237 173 H295" stroke="#696f87" stroke-width="1.1"/>
    <path d="M9 151 15 133 21 151Z M297 154 305 129 313 154Z" fill="#435b72"/>
  {/if}
</g>
