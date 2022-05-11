' ===============================================================
' ZVLM       Virtual Pinball X Light Mapper generated code
'
' This file provide default implementation and template to add bakemap
' & lightmap synchronization for position and lighting.
'


' ===============================================================
' The following code can be copy/pasted if using Lampz fading system
' It links each Lampz lamp/flasher to the corresponding light and lightmap

Sub UpdateLightMap(lightmap, intensity, ByVal aLvl)
   if Lampz.UseFunction then aLvl = Lampz.FilterOut(aLvl)	'Callbacks don't get this filter automatically
   lightmap.Opacity = aLvl * intensity
End Sub

Sub LampzHelper
	' Sync on gi4
	Lampz.Callback(0) = "UpdateLightMap Playfield_LM_All_Lights, 400.00, "
	Lampz.Callback(0) = "UpdateLightMap Parts_LM_All_Lights, 400.00, "
	Lampz.Callback(0) = "UpdateLightMap LeftInlane_LM_All_Lights, 400.00, "
	Lampz.Callback(0) = "UpdateLightMap RightInlane_LM_All_Lights, 400.00, "
End Sub


' ===============================================================
' The following code can be copy/pasted to disable baked lights
' Lights are not removed on export since they are needed for ball
' reflections and may be used for lightmap synchronisation.

Sub HideLightHelper
	VPX.Env.Visible = False
	gi1.Visible = False
	gi2.Visible = False
	gi3.Visible = False
	gi4.Visible = False
End Sub


' ===============================================================
' The following code can serve as a base for movable position synchronization.
' You will need to adapt the part of the transform you want to synchronize
' and the source on which you want it to be synchronized.

Sub MovableHelper
End Sub


' ===============================================================
' The following provides a basic synchronization mechanism were
' lightmaps are synchronized to corresponding VPX light or flasher,
' using a simple realtime timer called VLMTimer. This works great
' as a starting point but Lampz direct lightmap fading shoudl be prefered.

Sub VLMTimer_Timer
	UpdateLightMapFromLight gi4, Playfield_LM_All_Lights, 400.00, False
	UpdateLightMapFromLight gi4, Parts_LM_All_Lights, 400.00, False
	UpdateLightMapFromLight gi4, LeftInlane_LM_All_Lights, 400.00, False
	UpdateLightMapFromLight gi4, RightInlane_LM_All_Lights, 400.00, False
End Sub

Function LightFade(light, is_on, percent)
	If is_on Then
		LightFade = percent*percent*(3 - 2*percent) ' Smoothstep
	Else
		LightFade = 1 - Sqr(1 - percent*percent) ' 
	End If
End Function

Sub UpdateLightMapFromFlasher(flasher, lightmap, intensity_scale, sync_color)
	If flasher.Visible Then
		If sync_color Then lightmap.Color = flasher.Color
		lightmap.Opacity = intensity_scale * flasher.IntensityScale * flasher.Opacity / 1000.0
	Else
		lightmap.Opacity = 0
	End If
End Sub

Sub UpdateLightMapFromLight(light, lightmap, intensity_scale, sync_color)
	light.FadeSpeedUp = light.Intensity / 50 '100
	light.FadeSpeedDown = light.Intensity / 200
	If sync_color Then lightmap.Color = light.Colorfull
	Dim t: t = LightFade(light, light.GetInPlayStateBool(), light.GetInPlayIntensity() / (light.Intensity * light.IntensityScale))
	lightmap.Opacity = intensity_scale * light.IntensityScale * t
End Sub
