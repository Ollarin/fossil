'''
This is how rigs are actually created.

When defining controls, they must all follow this pattern:

    @defaultspec( {'shape': control.box,    'size': 10, 'color':'green 0.22' },
               pv={'shape': control.sphere, 'size': 5,  'color':'green 0.92' } )
    someControl(startJoint, endJoint, <keyword Args altering behavior>, name='', groupName='', controlSpec={} )
        return control, container
    
        `name` (Likely for most IkControls only) will be name of the control, respecting the suffix.
        
        `groupName` is the optional subgroup the control parts be put under, purely for organization.
            It falls back to the visGroup of the 'main' controller.
        `controlSpec` will be filled and can have parts overridden.  This works
            in conjunction with the `defaultspec` decorator
            
        Returns:
        `control` is the 'main' control.  Additional controls get added it it
            via control.subControl['some name'] = other control.

        `container` is the group that is made to hold all the junk under the main controller.
        

..  todo:: BUGS

    *** I REALLY NEED TO CHECK FOR THE SIDE SUFFIX!  It's super weird when something doesn't build.
        I could also do some work on deriving the rig/rigMirror by looking for the existing joint
        that's a child of the expected root joint.
        * Maybe cards also inherit parent card suffix!
        
    * ik need fk built along with them
        * And then match them
            * across time
        
    * Move mirror tagging from joints to cards completely
        
    * shape didn't appear to change for pin

    * Adding splineIk to naga chose the wrong axis to plug into spinners

    * The alignment of the dogleg bend is off IF the card is not on axis in some ways.
        I'm not sure of the exact conditions but if I leave the card at 90 rotations, everything appears fine.
        
    * Need to address when something is mirrored but doesn't have a suffix.
        
..  todo::
    * Make an -align flag for controls.  This also might entail some mirroring
    
    * Put all the controls in a group just like ikChain is done.
    
    Show anticipated controllers!

    Stretchy IK is editable (probably just expose as off and on)

    Tool to easily make a blueprint that matches an existing skeleton
    (probably will want to add tip joints to most things).
    
    If the neck is more than 1 joint, the last joint builds wrong with Zoo...
    Align hand control to hand joint.

    Each rig has a list of criteria so it's easy to test if rigging will actually work or not.
    Figure out why the last neck isn't connected to spine, must spine exist first?
    Joe said arms won't get made unless unless the spine exists, verify this is the case.

    Need to add FK to hindleg
    Need to add tail control like Joe has.
        Need to add FK control to it too.
        
    hindleg needs to not make the human foot.
    Need to make animal foot.
    
'''
from __future__ import absolute_import

import collections
import functools
import math
import re

from pymel.core import *

from maya.api import OpenMaya
import maya.OpenMayaAnim
import maya.OpenMaya

from ...add import *
from ...core.dagObj import lockRot, lockTrans, lockScale
from ... import core
from ... import lib
from ... import nodeApi

from . import controller
from . import log
from . import space
from . import util

mel.ikSpringSolver()

CONTROL_ATTR_NAME = 'influence'


def intersect(mesh, point, ray):
    fnMesh = OpenMaya.MFnMesh( core.capi.asMObject(mesh.getShape()).object() )

    p = spaceLocator()
    p.t.set(point)
    p.setParent( mesh )
    
    objSpacePos = p.t.get()

    p.setTranslation( ray, space='world' )

    objSpaceRay = p.t.get() - objSpacePos
    
    point = OpenMaya.MFloatPoint(objSpacePos)
    ray = OpenMaya.MFloatVector(objSpaceRay)
    res = fnMesh.allIntersections(point, ray, OpenMaya.MSpace.kObject, 50, False )

    # -> (hitPoints, hitRayParams, hitFaces, hitTriangles, hitBary1s, hitBary2s)

    if not len(res[0]):
        hits = []
    
    else:
        hits = []
        for hit in res[0]:
            p.t.set( hit.x, hit.y, hit.z)
            hits.append( p.getTranslation(space='world') )
    
    delete(p)
    
    return hits


def _getSwitchPlug(obj):
    '''
    !DUPED!
    Given the object a bind joint is constrained to, return the switching plug.
    '''

    bone = core.constrains.getOrientConstrainee(obj)
    constraint = orientConstraint( bone, q=True )
    
    plugs = orientConstraint(constraint, q=True, wal=True)
    targets = orientConstraint(constraint, q=True, tl=True)
    
    for plug, target in zip(plugs, targets):
        if target == obj:
            switchPlug = plug.listConnections(s=True, d=False, p=True)
            return switchPlug

    
def _getActiveControl(outputSide):
    if outputSide.ik and outputSide.fk:
        plug = _getSwitchPlug(outputSide.fk)[0]
        
        if plug.get() > 0.5:
            return outputSide.fk
        else:
            return outputSide.ik
    else:
        if outputSide.ik:
            return outputSide.ik
        else:
            return outputSide.fk

    
def getMainController(obj):
    '''
    Given a controller, return the main RigController (possibly itself) or
    None if not found.
    '''
    if isinstance(obj, nodeApi.RigController):
        return obj
    else:
        objs = obj.message.listConnections(type=nodeApi.RigController)
        if objs:
            return objs[0]
    
    return None
    
#pymel.internal.factories.registerVirtualClass( RigController )


def constrainTo(constrainee, target):
    '''
    zoo does orient and point but it is possible a parent would work just as well.
    It is also possible the "rotation only" is the reason they are separate but
    I'll want to prove this is the case before changing anything.
    
    :return:
    '''
    
    o = orientConstraint( target, constrainee, mo=True )
    p = pointConstraint( target, constrainee, mo=True )
    
    return o.getWeightAliasList()[-1], p.getWeightAliasList()[-1]


def drive(control, attr, driven, minVal=None, maxVal=None, asInt=False, dv=None):
    '''
    Add the attr to the control and feed it into driven.
    '''
    
    attrType = 'short' if asInt else 'double'
    
    if not control.hasAttr( attr ):
        control.addAttr( attr, at=attrType, k=True )
        if minVal is not None:
            control.attr( attr ).setMin(minVal)
        if maxVal is not None:
            control.attr( attr ).setMax(maxVal)
        
        if dv is not None:
            defaultVal = dv
            if maxVal is not None:
                defaultVal = min(defaultVal, maxVal)
            if minVal is not None:
                defaultVal = max(defaultVal, minVal)
            addAttr(control.attr(attr), e=True, dv=dv)

    control.attr(attr) >> driven
    
    return control.attr(attr)


def prune(start, end, trimEnd=True):
    '''
    Cut the joint chain to just the start and end joints, no branching.
    
    :param bool trimEnd: True by default, removing any children of `end`.
    '''
    p = end.getParent()
    keep = end
    
    if trimEnd:
        ends = end.listRelatives(type='transform')
        if ends:
            delete(ends)
    
    if not end.longName().startswith( start.longName() ):
        raise Exception( "{0} is not a descendant of {1}".format( end, start) )
    
    while True:
        for child in p.listRelatives():
            if child != keep:
                delete(child)
                
        keep = p
        p = p.getParent()
        
        if keep == start:
            return


def findChild(chain, target):
    '''
    Given a joint chain, find the child of the target name
    '''
    
    for child in chain.listRelatives(type='joint'):
        if child.name().rsplit('|')[-1] == target:
            return child

    for child in chain.listRelatives(type='joint'):
        t = findChild(child, target)
        if t:
            return t
            
    return None


def dupChain(start, end, nameFormat='{0}_dup'):
    '''
    Creates a duplicate chain, pruned of all branches and children.  Can handle
    same joints and start and end.
    
    :param string nameFormat: The str.format used on the duped chain
    
    '''
    
    chain = getChain(start, end)
    
    assert chain, '{0} and {1} are not in the same hierarchy, dupChain() failed'.format(start, end)
    
    dup = duplicate(start)[0]
    
    if start != end:
        child = findChild( dup, simpleName(end) )
        assert child, 'dupChain failed to find duped child {0} in {1}'.format(end, start)
        prune( dup, child )
    else:
        child = dup
    
    dupChain = getChain( dup, child )
    
    ends = dupChain[-1].getChildren(type='transform')
    if ends:
        delete(ends)
    
    for src, d in zip(chain, dupChain):
        dupName = simpleName(src, nameFormat)
        d.rename(dupName)
    return dupChain
        

def countJoints(start, end):
    count = 2
    
    p = end.getParent()
    
    while p and p != start:
        p = p.getParent()
        count += 1
        
    if not p:
        return 0
        
    return count


def getDepth(jnt, depth):
    current = 1
    
    child = jnt
    while current < depth:
        child = child.listRelatives(type='joint')[0]
        current += 1
        
    return child


def adds(*attributes):
    '''
    Marks a function with motigaDynamicAttrs to track the attributes made so
    special sauce can be identified.
    '''
    def realDecorator(func):
        setattr(func, 'motigaDynamicAttrs', attributes)
        return func
    
    return realDecorator


def defaultspec(defSpec, **additionalSpecs):
    '''
    Decorator to used to specify the default control building values.
    
    ex:
        @defaultspec( {'shape':control.box, 'size', 10: 'color': 'blue 0.22'} )
        def buildLeg( ... , controlSpec={})
            ...
            control.build( 'FootControl', controlsSpec['main'] )
            
    Or, for multiple controls:
        @defaultspec( {'shape':control.box, 'size', 10: 'color': 'blue'},
            pv={'shape':control.sphere, 'size', 8: 'color': 'green'})
        def buildLegIk( ... , controlSpec={})
            ...
            control.build( 'FootControl', controlsSpec['main'] )
            ...
            control.build( 'FootControl', controlsSpec['pv'] ) # Same keyword as was passed in to defaultspec
    
    
    The reason is this allows for partial overriding, if a value isn't specifice,
    the default is used.  This also saves from having a long default argument
    list which varies from control to control.
    
    If some aspect of a rig adds an additional control, it is trivial to add it
    as a spec into the system.
    
    ..  todo::
        I might want to log spec errors is some better way to show them all at the end
    '''

    def realDecorator(func):
        # allSpecs will be an alterable, the source remains untouched.
        allSpecs = { 'main': defSpec.copy() }
        if 'visGroup' not in allSpecs['main']:
            allSpecs['main']['visGroup'] = ''
        if 'align' not in allSpecs['main']:
            allSpecs['main']['align'] = 'y'
        
        for specName, spec in additionalSpecs.items():
            allSpecs[specName] = spec.copy()
            if 'visGroup' not in allSpecs[specName]:
                allSpecs[specName]['visGroup'] = ''
            if 'align' not in allSpecs[specName]:
                allSpecs[specName]['align'] = 'y'
                
        def newFunc(*args, **kwargs):
            
            # Make a copy of the spec that can be modified
            tempSpec = {}
            for specName, spec in allSpecs.items():
                tempSpec[specName] = spec.copy()
            
            # Override default controlSpecs with whatever the user provides
            if 'controlSpec' in kwargs:
                # Apply any overridden spec data
                for specName, spec in kwargs['controlSpec'].items():
                    if specName in tempSpec:
                        tempSpec[specName].update( spec )
                    else:
                        warning( 'Ignoring unused spec {0}'.format(specName) )
                
            kwargs['controlSpec'] = tempSpec
                
            #argspec = inspect.getargspec(func)
            #print argspec
            #print args, kwargs
                
            res = func(*args, **kwargs)
            
            # Now that all the controls are made, we can safely apply the
            # visGroup, since they apply to the '_space' group, not the actual
            # control which is connected to the ik/fk switch attr
            if tempSpec['main']['visGroup']:
                lib.sharedShape.connect(res[0], (tempSpec['main']['visGroup'], 1) )
            
            subControls = res[0].subControl.items()
            if subControls:
                
                # If there is one spec and sub controls, it is a chain so apply the same visgroup
                if len(tempSpec) == 1 and tempSpec['main']['visGroup']:
                    for name, ctrl in subControls:
                        lib.sharedShape.connect(ctrl, (tempSpec['main']['visGroup'], 1) )
            
                # If there are 2 specs, the non-main is the repeating one
                elif len(tempSpec) == 2:
                    specName = tempSpec.keys()[:].remove('main')
                    visGroup = tempSpec['main']['visGroup']
                    if visGroup:
                        for name, ctrl in subControls:
                            lib.sharedShape.connect(ctrl, (visGroup, 1) )
                
                # Finally, each additional spec should match a sub control
                else:
                    for specName in tempSpec:
                        if specName == 'main':
                            continue
                        
                        if tempSpec[specName]['visGroup']:
                            try:  # &&& Eventually this needs to not ignore errors
                                lib.sharedShape.connect(
                                    res[0].subControl[specName],
                                    (tempSpec[specName]['visGroup'], 1)
                                )
                            except:
                                pass
            
            return res
        # Store the default spec so it's easy to access for other things.
        setattr( newFunc, '__defaultSpec__', allSpecs )
        functools.update_wrapper( newFunc, func )
        return newFunc
        
    return realDecorator
        
   
def calcOutVector(start, middle, end):
    '''
    Given the lead joint of 3, determine the vector pointing directly away along the xz plane.
    
    ..  todo::
        Gracefully handle if the ik is on the xz plane already.
    '''

    s = dt.Vector( xform(start, q=1, ws=1, t=1) )
    m = dt.Vector( xform(middle, q=1, ws=1, t=1) )
    e = dt.Vector( xform(end, q=1, ws=1, t=1) )

    up = s - e

    kneeScale = ( m.y - e.y ) / up.y if up.y else 0.0
    
    modifiedUp = kneeScale * up
    newPos = modifiedUp + e

    outFromKnee = m - newPos
    
    angleBetween = (m - s).angle( e - m )

    log.TooStraight.check(angleBetween)


    outFromKnee.normalize()
    
    return outFromKnee


def calcOutVectorRaw(start, middle, end):
    '''
    Same as calcOutVector but isn't constrained to xz plane.
    '''

    s = dt.Vector( xform(start, q=1, ws=1, t=1) )
    m = dt.Vector( xform(middle, q=1, ws=1, t=1) )
    e = dt.Vector( xform(end, q=1, ws=1, t=1) )

    out = (m - s) + (m - e)
    out.normalize()

    return out


def bugleg(start, end):
    '''
    Make chain, spring IK across whole thing
    Replicate dogleg roll at both ends
    
    '''
    pass


def _makeStretchyPrep(controller, ik, stretchDefault=1):
    start = ik.startJoint.listConnections()[0]
    end = ik.endEffector.listConnections()[0].tz.listConnections()[0]
    chain = getChain( start, end )
    jointAxis = identifyAxis( end )
    
    switcher = createNode('blendTwoAttr', n='stretchSlider')
    switcher.input[0].set(1)

    drive(controller, 'stretch', switcher.attributesBlender, minVal=0, maxVal=1, dv=max(min(stretchDefault, 1), 0) )
    controller.stretch.set(1)
    
    controller.addAttr('modAmount', at='double', k=False)
    controller.modAmount.set(cb=True)
    chainMeasure(chain) >> controller.modAmount
    
    return start, chain, jointAxis, switcher


def makeStretchySpline(controller, ik, stretchDefault=1):
    start, chain, jointAxis, switcher = _makeStretchyPrep( controller, ik, stretchDefault )
    
    crv = ik.inCurve.listConnections()[0]
    length = arclen(crv, ch=1).arcLength
    lengthMax = arclen(crv, ch=1).arcLength.get()
    # Spline squashes and stretches
    multiplier = core.math.divide( length, lengthMax )
    
    jointLenMultiplier = switcher.output
    
    multiplier >> switcher.input[1]
    
    for i, j in enumerate(chain[1:], 1):
        util.recordFloat(j, 'restLength', j.attr('t' + jointAxis).get() )
        core.math.multiply( jointLenMultiplier, j.restLength) >> j.attr('t' + jointAxis)
    
    return controller.attr('stretch'), jointLenMultiplier
    
    
def makeStretchyNonSpline(controller, ik, stretchDefault=1):
    start, chain, jointAxis, switcher = _makeStretchyPrep( controller, ik, stretchDefault )

    dist, grp = core.dagObj.measure(start, ik)
    grp.setParent( controller )
    dist.setParent( ik )
    length = dist.distance
    #lengthMax = sum( [abs(j.attr('t'+jointAxis).get()) for j in chain[1:] ] )
    lengthMax = chainLength(chain)
    # Regular IK only stretches
    # ratio = (abs distance between start and end) / (length of chain)
    ratio = core.math.divide( length, lengthMax )
    # multiplier is either 1 or a number greater than one needed for the chain to reach the end.
    multiplier = core.math.condition( ratio, '>', 1.0, true=ratio, false=1 )

    controller.addAttr( 'length', at='double', min=-10.0, dv=0.0, max=10.0, k=True )

    '''
    lengthMod is the below formula:

    if controller.length >= 0:
        controller.length/10.0 + 1.0 # 1.0 to 2.0 double the length of the limb
    else:
        controller.length/20.0  + 1.0 # .5 to 1.0 halve the length of the limb
    '''
    lengthMod = core.math.add(
        core.math.divide(
            controller.length,
            core.math.condition(controller.length, '>=', 0, 10.0, 20.0)
        ),
        1.0
    )
    
    jointLenMultiplier = core.math.multiply(switcher.output, lengthMod)
    
    multiplier >> switcher.input[1]
    
    for i, j in enumerate(chain[1:], 1):
        util.recordFloat(j, 'restLength', j.attr('t' + jointAxis).get() )
            
        attrName = 'segLen' + str(i)
        
        # Make an attribute that is -10 to 10 map to multiplying the restLength by 0 to 2
        controller.addAttr( attrName, at='double', k=True, min=-10, max=10 )
        normalizedMod = core.math.add(core.math.divide( controller.attr(attrName), 10), 1)
        
        core.math.multiply(
            jointLenMultiplier,
            core.math.multiply( normalizedMod, j.restLength)
        ) >> j.attr('t' + jointAxis)
    
    return controller.attr('stretch'), jointLenMultiplier


ConstraintResults = collections.namedtuple( 'ConstraintResults', 'point orient' )


def constrainAtoB(chain, controlChain, mo=True):
    '''
    Point/orient constraint the first chain to the second, driving all their
    weights by the lead joint.
    '''
    points = []
    orients = []
    for _controller, orig in zip( controlChain, chain ):
        points.append( pointConstraint( _controller, orig, mo=mo ).getWeightAliasList()[-1] )
        orients.append( orientConstraint( _controller, orig, mo=mo ).getWeightAliasList()[-1] )
    
    for p in points[1:]:
        points[0] >> p
        
    for o in orients[1:]:
        orients[0] >> o
        
    return ConstraintResults(points[0], orients[0])


def driveConstraints(srcConstraintResult, destConstraintResult):
    '''
    Have the destConstraintResult controlled by the source.
    
    Intended use is for chains where some joints, likely the tip, are constrained
    to the controller instead of the drive chain
    '''
    
    srcConstraintResult.point >> destConstraintResult.point
    srcConstraintResult.orient >> destConstraintResult.orient
    

# Spline
def ___addControllers():
    jnts = listRelatives(ad=True) + selected()
    for j in jnts:

        g = group(em=1)
        g.setParent(j)
        g.t.set(0, 0, 0)
        core.math.divide( j.rz, -1 ) >> g.rz
        dup = duplicate(ctrl)[0]
        dup.setParent(g)
        dup.t.set(0, 0, 0)


def addConnectingCurve(objs):
    '''
    Given a list of objects, make a curve that links all of them.
    '''
    crv = curve( d=1, p=[(0, 0, 0)] * len(objs) )

    grp = group(crv, n='connectingCurve')

    for i, obj in enumerate(objs):
        handle = cluster(crv.cv[i])[1]
        pointConstraint( obj, handle )
        handle.setParent( grp )
        hide(handle)
        
    crv.getShape().overrideEnabled.set( 1 )
    crv.getShape().overrideDisplayType.set( 2 )
        
    return grp


def addC(ctrl, target):
    '''
    Puts a `ctrl` on each child joint of the selected joints
    Target is a mirror list of the bound joints
    '''
    #expression -e -s "//\njoint5.rotateZ = nurbsCircle21.rotateZ + (nurbsCircle22.rz + nurbsCircle20.rotateZ)*.5;"  -o joint5 -ae 1 -uc all  expression2;
    obj = selected()[0]
        
    controls = []
    groups = []
    
    while obj:
    
        c = duplicate(ctrl)[0]
        c.setParent(obj)
        c.t.set(0, 0, 0)
        
        controls.append(c)
        
        spinner = group(em=True)
        spinner.setParent(obj)
        spinner.t.set(0, 0, 0)
        
        groups.append(spinner)
        
        pointConstraint( obj, target )
        orientConstraint( spinner, target )
        
        children = obj.listRelatives(type='joint')
        if children:
            obj = children[0]
        else:
            obj = None
            break

        target = target.listRelatives(type='joint')[0]
    
    for i, s in enumerate(groups[2:-2], 2):
        msg = '{0}.rz = {1[2]}.rz + ( {1[1]}.rz + {1[3]}.rz ) * 0.5 +  ( {1[0]}.rz + {1[4]}.rz ) * 0.2;'.format( s, controls[i - 2:i + 3] )
        expression( s=msg )
    
    msg = '{0}.rz = {1[0]}.rz + ( {1[1]}.rz ) * 0.5 +  ( {1[2]}.rz ) * 0.2;'.format( groups[0], controls[:3] )
    expression( s=msg )
    
    msg = '{0}.rz = {1[1]}.rz + ( {1[0]}.rz + {1[2]}.rz ) * 0.5 +  ( {1[3]}.rz ) * 0.2;'.format( groups[1], controls[:4] )
    expression( s=msg )
        
    msg = '{0}.rz = {1[2]}.rz + ( {1[1]}.rz ) * 0.5 +  ( {1[0]}.rz ) * 0.2;'.format( groups[-1], controls[-3:] )
    expression( s=msg )
    
    msg = '{0}.rz = {1[2]}.rz + ( {1[1]}.rz + {1[3]}.rz ) * 0.5 +  ( {1[0]}.rz ) * 0.2;'.format( groups[-2], controls[-4:] )
    expression( s=msg )
    
    
def addTwistControls(controlChain, boundChain, boundEnd, influenceDist=3):
    '''
    Put a rotation controller under each child of the controlChain to drive .rz
    of the boundChain.  They must both be the same size.
    
    :param Joint controlChain: The first joint of the controlling rig (ideally pruned)
    :param Joint boundChain: The first joint of joints being controlled by the spline.
    :param Joint boundEnd: The last joint in the bound chain, used to address possible branching.
    :param int influenceDist: How many adjacent joints are influenced (total #
        is 2x since it influences both directions).
    '''
    
    obj = controlChain[0]
    target = boundChain
    
    #controlJoints = getChain( controlChain, findChild(controlChain, shortName(boundEnd)) )
    controlJoints = controlChain
    boundJoints = getChain( boundChain, findChild(boundChain, shortName(boundEnd)) )
    
    assert len(controlJoints) == len(boundJoints), "Failure when adding twist controls, somehow the chains don't match length, contorls {0} != {1}".format( len(controlJoints), len(boundJoints) )
    
    controls = []
    groups = []

    pointConstraints = []
    orientConstraints = []
    
    for i, (obj, target) in enumerate(zip(controlJoints, boundJoints)):
    
        c = controller.control.circle()
        c.setParent(obj)
        c.t.set(0, 0, 0)
        c.r.set(0, 0, 0)
        
        controls.append(c)
        
        spinner = group(em=True, name='spinner%i' % i, p=target)
        spinner.r.set(0, 0, 0)
        spinner.setParent(obj)
        spinner.t.set(0, 0, 0)
        
        # Aligning the spinners to the bound joint means we don't have to offset
        # the orientConstraint which means nicer numbers.
#        spinner.setRotation( target.getRotation(space='world'), space='world' )
        
        groups.append(spinner)

        pointConstraints.append( core.constraints.pointConst( obj, target, maintainOffset=False ) )
        orientConstraints.append( core.constraints.orientConst( spinner, target, maintainOffset=False ) )
        
        children = obj.listRelatives(type='joint')
        if children:
            obj = children[0]
        else:
            obj = None
            break
    
    for pSrc, pDest in zip( pointConstraints[:-1], pointConstraints[1:]):
        pSrc >> pDest
    
    for oSrc, oDest in zip( orientConstraints[:-1], orientConstraints[1:]):
        oSrc >> oDest
    
    # &&& This and the i+7 reflect the number of controls that influence
    bigList = [None] * influenceDist + controls + [None] * influenceDist
    
    influenceRange = (influenceDist * 2) + 1
    
    axis = identifyAxis(controlChain[0].listRelatives(type='joint')[0])
    
    exp = []
    for i, spinner in enumerate(groups):
        exp.append(driverExpression( spinner, bigList[i: i + influenceRange], axis ))
        
    expression( s=';\n'.join(exp) )
    
    return controls, ConstraintResults( pointConstraints[0], orientConstraints[0] )


def calcInfluence( controls ):
    '''
    Given a list (Maybe change to a number?) returns a list of power falloffs.
    
    controls can have None placeholders
    
    power falls off to end of controls
    low   upper
      v   v
    0 1 2 3 4
    # Result: [0.5, 0.75, 1.0, 0.75, 0.5]
    
    low     upper
      v     v
    0 1 2 3 4 5
    # Result: [0.5, 0.75, 1.0, 1.0, 0.75, 0.5]
    
    '''
    max = len(controls)
    if len(controls) % 2 == 0:
        upper = len(controls) / 2 + 1
        lower = upper - 2
    else:
        upper = len(controls) / 2 + 1
        lower = upper - 1
        
    delta = 1 / float(lower) * 0.5
        
    powers = [1.0] * len(controls)
    #for i, (lowCtrl, upCtrl) in enumerate(zip(controls[upper:], reversed(controls[:lower]) ), 1):
    for i, (lowCtrl, upCtrl) in enumerate(zip(range(upper, max), range( lower - 1, -1, -1 ) ), 1):
        power = 1 - delta * i
        powers[lowCtrl] = power
        powers[upCtrl] = power

    return powers


def driverExpression( driven, controls, axis ):
    '''
    The `driven` node's .rz will be driven by the list of `controls`.
    `controls` is a list of objects, and optional empty entries.
    
    Example, if you have joints, A B C and controls X Y Z, you would do:
        driverExpression( A, [None, X, Y] )
        driverExpression( B, [X, Y, Z] )
        driverExpression( C, [Y, Z, None] )
    
    This means A will be fully influenced by X, and partially by Y.
    B is fully influenced by Y and partially by X and Z.
    '''
    
    powers = calcInfluence(controls)
    exp = []
    for power, ctrl in zip(powers, controls):
        if ctrl:
            exp.append( '{0}.r{axis} * {1}'.format(ctrl, power, axis=axis) )
    
    return '{0}.r{axis} = {1};'.format( driven, ' + '.join(exp), axis=axis )


def addControlsToCurve(name, crv=None,
    spec={'shape': controller.control.sphere, 'size': 10, 'color': 'blue 0.22'} ):  # noqa e128
    '''
    Given a curve, make a control sphere at each CV.
    
    :return: List of newly made controls.
    '''
    if not crv:
        crv = selected()[0]

    controls = []
        
    for i, cv in enumerate(crv.cv):
        #l = control.sphere( '{0}{1:0>2}'.format( name, i+1), size, 'blue', type=control.SPLINE )
        l = controller.control.build('{0}{1:0>2}'.format(name, i + 1), spec, type=controller.control.SPLINE)
        
        core.dagObj.moveTo( l, cv )
        handle = cluster(cv)[1]
        handle.setParent(l)
        hide(handle)
        controls.append(l)

    return controls


def makeTestJoints(raw=True):
    geom = selected()[0]

    prevJ = None
    ctrlJ = None

    joints = []
    
    for i in range(10):
        n = duplicate(geom)[0]
        select(cl=1)
        j = joint()
        joints.append(j)
        j.tz.set(-1 * i)
        n.setParent(j)
        n.t.set(0, 0, 0)
        
        if prevJ:
            j.setParent(prevJ)
        prevJ = j
        
        if raw:
            continue
        
        gj = j
        
        select(cl=1)
        j = joint()
        j.tz.set(-2 * i)
        j.ty.set(4)
        
        if ctrlJ:
            j.setParent(ctrlJ)
        ctrlJ = j
        
        orientConstraint( j, gj, mo=1, sk='z' )
        j.tz >> gj.tz
    
            
def findDepth(start, end):
    '''
    Find how many joints deep the end is from the start.  This is done in terms
    of overall length so a if end is the child of start, it will return 2, if
    it is a grandchild, 3 etc.
    '''
    depth = 2
    p = end.getParent()
    while p and p != start:
        p = p.getParent()
        depth += 1
        
    if not p:
        raise Error( end + ' is not a descendent of ' + start  )
        
    return depth


def identifyAxis(jnt, asVector=False):
    '''
    Determines the primary axis of the joint in relation to its parent,
    returning 'x', 'y' or 'z' or the appropriate vector if asVector is True.
    '''
        
    jointAxis = max( zip( [abs(n) for n in jnt.t.get()], 'xyz' ) )[1]
    
    if asVector:
        jointAxis = {'x': [1, 0, 0], 'y': [0, 1, 0], 'z': [0, 0, 1]}[jointAxis]
        
    return jointAxis


def getIkGroup():
    '''
    DEPRECATED
    Makes, if needed, and returns the group holding ik controls
    '''
    
    for child in lib.getNodes.mainGroup().listRelatives():
        if shortName(child) == 'ikParts':
            return child
    
    return group(em=True, name='ikParts', p=lib.getNodes.mainGroup())


def getControlGroup(name):
    '''
    Used to organize controls under the main group.
    '''
    match = re.match( '[_a-zA-Z]+[_a-zA-Z0-9_]*', name )

    if not match or match.group(0) != name:
        raise Exception( "An invalid group name was given" )
    
    for child in lib.getNodes.mainGroup().listRelatives():
        if shortName(child) == name:
            return child
    
    g = group(em=True, name=name, p=lib.getNodes.mainGroup())
    lockRot(g)
    lockScale(g)
    lockTrans(g)
    return g


def parentProxy(target):
    '''
    Makes a group that follows the parent so children of this newly created group
    will behave as children of the target group.
    
    ..  todo::
        Replace with parentGroup, which read better in a hierarchy to know what is going on
    '''
    
    name = target.name() + '_Proxy'
    
    for child in lib.getNodes.mainGroup().listRelatives():
        if shortName(child) == name:
            return child
    
    grp = group( em=True )
    grp.rename( name )
    grp.setParent( lib.getNodes.mainGroup() )
    
    parentConstraint( target, grp, mo=False )
    
    return grp


def parentGroup(target):
    '''
    Returns a group that is constrained to the parent of the target.
    Used to allow control hierarchies to live elsewhere.
    
    ..  todo::
        Get rid of parentProxy, which is dumb
    '''
    
    name = simpleName(target, '{0}_Proxy' )
    grp = group( em=True, name=name )

    # Don't constrain top level nodes since they need to follow main, not b_Root
    if target.getParent() != core.findNode.getRoot():
        parentConstraint( target.getParent(), grp, mo=False )

    return grp
    

def getChain(start, end):
    '''
    Returns a list of joints from start to end or an empty list if end isn't
    descended from start.
    '''
    
    joints = []
    current = end
    while current and current != start:
        joints.append( current )
        current = current.getParent()
        
    # If we never hit the start, start and end are unrelated.
    if current != start:
        return []
        
    joints.append( start )
    joints.reverse()
    
    return joints


def trimName(jnt):
    '''
    Given an joint, return its simple name without b_ or rig_ if those prefixes exist.
    '''
    name = simpleName(jnt)
    if name.startswith( 'b_' ):
        return name[2:]
    
    return name


def storeTrueZero(obj, rot):
    '''
    '''
    obj.addAttr( 'trueZero', at='double3' )
    
    obj.addAttr( 'trueZeroX', at='double', p='trueZero' )
    obj.addAttr( 'trueZeroY', at='double', p='trueZero' )
    obj.addAttr( 'trueZeroZ', at='double', p='trueZero' )

    obj.trueZero.set( channelBox=True )
    obj.trueZeroX.set( channelBox=abs(rot[0]) > 0.00000000001 )
    obj.trueZeroY.set( channelBox=abs(rot[1]) > 0.00000000001 )
    obj.trueZeroZ.set( channelBox=abs(rot[2]) > 0.00000000001 )
    obj.trueZero.set( rot )
    obj.trueZero.lock()
    obj.trueZeroX.lock()
    obj.trueZeroY.lock()
    obj.trueZeroZ.lock()
    '''
    
    for val, attr in zip(rot, 'XYZ'):
        obj.addAttr( 'trueZero' + attr, at='double', p='trueZero' )
        obj.attr( 'trueZero' + attr).set( channelBox=True )
        obj.attr( 'trueZero' + attr).set( val )
        obj.attr( 'trueZero' + attr).lock()
        
    obj.trueZero.set( channelBox=True )
    obj.trueZero.lock()'''


def determineClosestWorldOrient(obj):
    '''
    Given an object, returns the shortest rotation that aligns the object with
    the world.  This is used to allow IK elements to have world alignment but
    easily return to the bind pose.
    '''

    ''' # This is essentially a math version of the following:
        x = spaceLocator()
        y = spaceLocator()
        core.dagObj.moveTo( x, obj )
        core.dagObj.moveTo( y, obj )
        x.tx.set( 1 + x.tx.get() )
        y.ty.set( 1 + y.ty.get() )
        x.setParent(obj)
        y.setParent(obj)
        
        def zeroSmaller(loc):
            vals = [abs(v) for v in loc.t.get() ]
            largetVal = max(vals)
            index = vals.index(largetVal)
            for i, attr in enumerate('xyz'):
                if i == index:
                    continue
                loc.attr( 't' + attr ).set(0)
        
        zeroSmaller( x )
        zeroSmaller( y )
        
        ref = spaceLocator()
        core.dagObj.moveTo( ref, obj )
        aimConstraint( x, ref, wut='object', wuo=y )
        
        rot = ref.r.get()
        delete( x, y, ref )
        return rot
    '''

    # Make 2 world spaced points one unit along x and y
    x = dt.Matrix( [ (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (1, 0, 0, 0) ] )
    y = dt.Matrix( [ (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 1, 0, 0) ] )
    #z = dt.Matrix( [ (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 1, 0,) ] )
    
    world = obj.worldMatrix.get()
    inv = world.inverse()

    # Find the local matrices respective of the obj
    localX = x * inv
    localY = y * inv
    
    # For X, zero out the two smaller axes for each, ex t=.2, .3, .8 -> t=0, 0, .8
    def useX(matrix):
        return dt.Matrix( [matrix[0], matrix[1], matrix[2], [matrix[3][0], 0, 0, matrix[3][3]]] )
    
    def useY(matrix):
        return dt.Matrix( [matrix[0], matrix[1], matrix[2], [0, matrix[3][1], 0, matrix[3][3]]] )
        
    def useZ(matrix):
        return dt.Matrix( [matrix[0], matrix[1], matrix[2], [0, 0, matrix[3][2], matrix[3][3]]] )
    
    xUsed, yUsed, zUsed = [False] * 3
    if abs(localX[3][0]) > abs(localX[3][1]) and abs(localX[3][0]) > abs(localX[3][2]):
        localX = useX(localX)
        xUsed = True
    elif abs(localX[3][1]) > abs(localX[3][0]) and abs(localX[3][1]) > abs(localX[3][2]):
        localX = useY(localX)
        yUsed = True
    else:
        localX = useZ(localX)
        zUsed = True

    # Do the same for Y
    if xUsed:
        if abs(localY[3][1]) > abs(localY[3][2]):
            localY = useY(localY)
            yUsed = True
        else:
            localY = useZ(localY)
            zUsed = True
    
    elif yUsed:
        if abs(localY[3][0]) > abs(localY[3][2]):
            localY = useX(localY)
            xUsed = True
        else:
            localY = useZ(localY)
            zUsed = True
    
    elif zUsed:
        if abs(localY[3][0]) > abs(localY[3][1]):
            localY = useX(localX)
            xUsed = True
        else:
            localY = useY(localY)
            yUsed = True
    
    # Find the 'world' (but treating the obj's pos as the origin) positions.
    worldX = localX * world
    worldY = localY * world
    
    # Convert this into a rotation matrix by mimicing an aim constraint
    x = dt.Vector(worldX[-1][:-1])
    y = dt.Vector(worldY[-1][:-1])

    x.normalize()
    y.normalize()
    z = x.cross(y)
    y = z.cross(x)

    msutil = maya.OpenMaya.MScriptUtil()
    mat = maya.OpenMaya.MMatrix()
    msutil.createMatrixFromList([
        x[0], x[1], x[2], 0.0,
        y[0], y[1], y[2], 0.0,
        z[0], z[1], z[2], 0.0,
        0.0, 0.0, 0.0, 1.0
        ], mat) # noqa e123
    rot = maya.OpenMaya.MEulerRotation.decompose(mat, maya.OpenMaya.MEulerRotation.kXYZ)

    return dt.Vector(math.degrees( rot.x), math.degrees(rot.y), math.degrees(rot.z))


def shortestAxis(srcAngle):
    angle = abs(srcAngle) % 90
    
    if angle >= 89.99999:  # Due to float error, allow for some negligible slop to align the axis
        angle -= 90
    
    return math.copysign(angle, srcAngle)


def trueZeroSetup(rotationTarget, ctrl):
    '''
    Stores the closest world orient of the rotation target on the given control.

    ..  todo::
        Use this function in all the places where matchOrient exists.
    '''
    rot = determineClosestWorldOrient(rotationTarget)
    ctrl.r.set( rot )
    storeTrueZero(ctrl, rot)


def trueZeroFloorPlane(rotationTarget, ctrl):
    
    trans = xform(rotationTarget, q=True, ws=True, t=True)
    
    # Make a unit X vector (assume left side is +x, right is -x)
    if trans[0] >= 0:
        tx = dt.Matrix([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [1.0, 0.0, 0.0, 1.0]])
    else:
        tx = dt.Matrix([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [-1.0, 0.0, 0.0, 1.0]])
    
    # Move out from the rotator by the unit X vector (in world space)
    altered = tx * rotationTarget.worldMatrix.get()

    # Get the X and Z world position of the new point
    alteredX = altered[3][0]
    alteredZ = altered[3][2]

    # Find the difference in X and Z world positions to calc Y
    
    deltaX = alteredX - trans[0]
    deltaZ = alteredZ - trans[2]

    rad = math.atan2(deltaX, deltaZ)
    degrees = math.degrees(rad)
    ctrl.ry.set(degrees)
    storeTrueZero(ctrl, [0, degrees, 0])


def transferKeyableUserAttrs( src, dest ):
    '''
    '''
    for attr in src.listAttr( ud=True, k=True ):
        type = attr.get(type=True)
        kwargs = {}
        if type == 'enum':
            kwargs['enumName'] = ':'.join( attr.getEnums().keys() )
        dest.addAttr( attr.plugAttr(), at=type, k=True, **kwargs )
        newAttr = dest.attr( attr.plugAttr() )
        newAttr.set( attr.get() )
        newAttr >> attr
    
        newAttr.setMin( attr.getMin() )
        newAttr.setMax( attr.getMax() )


def createCurve():
    node = createNode('animCurveUU')
    ass = maya.OpenMayaAnim.MFnAnimCurve( core.capi.asMObjectOld(node) )

    ass.addKey(.5, -5)
    ass.addKey(1, 0)
    ass.addKey(2, 5)
        
        
def squashLinker(name, ctrlA, ctrlB):
    '''
    Name the control that will be made to handle the sizes of two squash controllers.
    '''

    temp = core.dagObj.zero(ctrlA, apply=False, make=False).getParent()
    aTarget = parentConstraint(temp, q=True, tl=True)[0]

    temp = core.dagObj.zero(ctrlB, apply=False, make=False).getParent()
    bTarget = parentConstraint(temp, q=True, tl=True)[0]

    if aTarget.fullPath() in bTarget.fullPath():
        child, parent = aTarget, bTarget
        childCtrl, parentCtrl = ctrlA, ctrlB
    elif bTarget.fullPath() in aTarget.fullPath():
        child, parent = bTarget, aTarget
        childCtrl, parentCtrl = ctrlB, ctrlA
    else:
        raise Exception( 'Selected controls do not map to related joints' )
    
    joints = getChain(child, parent)
        
    # Get the current distance along the bones to get the 'zeroed' value.
    total = 0
    lengthCalc = ''
    for j in joints[1:]:
        total += max( [abs(t) for t in j.t.get()] )
        lengthCalc += 'abs({0}.t{1}) + '.format(j, identifyAxis(j))
    
    lengthCalc = lengthCalc[:-3]
    
    ctrl = controller.control.build(name, {'shape': controller.control.sphere})
    zeroGrp = core.dagObj.zero(ctrl)

    pointConstraint(child, parent, zeroGrp)
    aimConstraint( child, zeroGrp )

    ctrl.ty.lock()
    ctrl.tz.lock()
    lockScale(ctrl)
    lockRot(ctrl)

    exp = ('{child}.size = 1.0 * ((1.0/ ({length}/{total}) )-1.0) + 1.0*{ctrl}.tx;\n' +
          '{parent}.size = 1.0 * ((1.0/ ({length}/{total}) )-1.0) - 1.0*{ctrl}.tx;') \
           .format( child=childCtrl, parent=parentCtrl, ctrl=ctrl, length=lengthCalc, total=total )
        
    print exp
        
    expression(s=exp)


def createMatcher(ctrl, target):
    '''
    Creates an object that follows target, based on ctrl so ctrl can match it
    easily.
    '''
    matcher = duplicate(ctrl, po=True)[0]
    parentConstraint( target, matcher, mo=True )

    matcher.rename( ctrl.name() + '_matcher' )
    hide(matcher)

    if not ctrl.hasAttr( 'matcher' ):
        ctrl.addAttr('matcher', at='message')

    matcher.message >> ctrl.matcher
    
    if matcher.hasAttr('motigaCtrlType'):
        matcher.deleteAttr( 'motigaCtrlType' )
    
    return matcher


def squashDrive(squashCtrl):
    '''
    Given a squash controller, have its size driven by the .tx of the joint it
    is built off of.
    '''

    # Find the bone this controls is built off of.
    zeroGrp = core.dagObj.zero(squashCtrl, make=0, apply=0)
    if not zeroGrp:
        return

    container = zeroGrp.getParent()
    targetBone = parentConstraint(container, q=True, tl=True)
    if not targetBone:
        return

    targetBone = targetBone[0]

    # Build setDrivenKey.
    setDrivenKeyframe( squashCtrl, at=['size'], cd=targetBone.tx )
    length = targetBone.tx.get()
    setDrivenKeyframe( squashCtrl, at=['size'], v=-5.0, cd=targetBone.tx, dv=[length * 2] )
    setDrivenKeyframe( squashCtrl, at=['size'], v=5.0, cd=targetBone.tx, dv=[length * .25] )


def chainLength(joints):
    return abs(sum( [j.tx.get() for j in joints[1:]] ))


def chainMeasure(joints):
    n = createNode('plusMinusAverage')
    n.operation.set(1)
    
    for i, j in enumerate(joints[1:]):
        j.tx >> n.input1D[i]
    
    l = chainLength(joints)
    if n.output1D.get() < 0:
        l *= -1
    
    return core.math.divide( n.output1D, l)


def advancedTwist(start, end, baseCtrl, endCtrl, ik):
    # Setup advanced twist
    startAxis = duplicate( start, po=True )[0]
    startAxis.rename( 'startAxis' )
    startAxis.setParent( baseCtrl )
    lockTrans(lockRot(lockScale(startAxis)))
    
    endAxis = duplicate( start, po=True )[0]
    endAxis.rename( 'endAxis' )
    endAxis.setParent( endCtrl )
    endAxis.t.set(0, 0, 0)
    lockTrans(lockRot(lockScale(endAxis)))
    
    hide(startAxis, endAxis)
    
    ik.dTwistControlEnable.set(1)
    ik.dWorldUpType.set(4)
    startAxis.worldMatrix[0] >> ik.dWorldUpMatrix
    endAxis.worldMatrix[0] >> ik.dWorldUpMatrixEnd


def midAimer(start, end, midCtrl, name='aimer', upVector=None):
    '''
    Creates an object point contrained to two others, aiming at the second.  Up
    vector defaults to the control's Y.
    '''
    aimer = group(em=True, name=name)
    #aimer.setParent(container)
    #aimer = polyCone(axis=[1, 0, 0])[0]
    core.dagObj.moveTo(aimer, midCtrl)
    pointConstraint(end, start, aimer, mo=True)
    
    
    aimV = dt.Vector(xform(end, q=True, ws=True, t=True)) - dt.Vector( xform(aimer, q=1, ws=1, t=1) )
    aimV.normalize()
    
    if upVector:
        midCtrlYUp = upVector
    else:
        temp = xform(midCtrl, q=True, ws=True, m=True)
        midCtrlYUp = dt.Vector( temp[4:7] )
    
    """
    # Generally the X axist is a good default up since things are normally  on that plane
    if abs(aimV[0]) < 0.0001 or min([abs(v) for v in aimV]) == abs(aimV[0]):
        upV = dt.Vector([-1, 0, 0])
        forwardV = aimV.cross(upV)
        recalcUp = forwardV.cross(aimV)
        
        # Reference
        #xrow = aimV
        #yrow = recalcUp
        #zrow = forwardV
        midCtrlYUp = recalcUp
        print 'midCtrlYUp', midCtrlYUp
    else:
        # Choose Y up as the up (hopefully this works)
        if abs(aimV[1]) < abs(aimV[0]) and abs(aimV[1]) < abs(aimV[2]):
            upV = dt.Vector([0, 1, 0])
            forwardV = aimV.cross(upV)
            recalcUp = forwardV.cross(aimV)
            
            # Reference
            #xrow = aimV
            #yrow = recalcUp
            #zrow = forwardV
            midCtrlYUp = recalcUp
            pass
    #
    """
    
    # Determine which axis of the end is closest to the midControl's Y axis.
    endMatrix = xform(end, q=True, ws=True, m=True)
    #midMatrix = xform(aimer, q=True, ws=True, m=True)
    #midCtrlYUp = dt.Vector(midMatrix[4:7])
    
    choices = [
        (endMatrix[:3], [1, 0, 0]),
        ([-x for x in endMatrix[:3]], [-1, 0, 0]),
        (endMatrix[4:7], [0, 1, 0]),
        ([-x for x in endMatrix[4:7]], [0, -1, 0]),
        (endMatrix[8:11], [0, 0, -1]),
        ([-x for x in endMatrix[8:11]], [0, 0, 1]),
    ]
    
    # Seed with the first choice as the best...
    low = midCtrlYUp.angle(dt.Vector(choices[0][0]))
    axis = choices[0][1]
    # ... and see if any others are better
    for vector, destAxis in choices[1:]:
        vector = dt.Vector(vector)  # Just passing 3 numbers sometimes gets a math error.
        
        if midCtrlYUp.angle(vector) < low:
            low = midCtrlYUp.angle(vector)
            axis = destAxis
    
    aimConstraint( end, aimer, wut='objectrotation', aim=[1, 0, 0], wuo=end, upVector=[0, 1, 0], wu=axis, mo=False)
    
    return aimer


def findClosest(obj, targets):
    '''
    Given an object or position, finds which of the given targets it is closest to.
    '''
    if isinstance(obj, (PyNode, basestring)):
        pos = xform(obj, q=True, ws=True, t=True)
    else:
        pos = obj
    
    dists = [((dt.Vector(xform(t, q=1, ws=1, t=1)) - pos).length(), t) for t in targets]
    dists.sort()
    return dists[0][1]


def twistSetup(control, twistJoints, startSegment, endSegment, twistLateralAxis=[0, 1, 0], driverLateralAxis=[0, 1, 0], defaultPower=0.5):
    '''
    Given a list of twist joints, an anchoring startSegment and the endSegment
    
    :param twistJoints: The joints that will be twisted
    :param twistDriver: The end joint that will influence the twisting
    
    
    TwistJoints bone's aim axis = the lateral axis
    TwistJoints Up axis = points to the target (wrist)

    Assumption, all the twist joints and start segment are oriented the same
        
    World up = object rotation
    up obj = target (wrist)
    up axis = I think this is the target's lateral axis

    '''
        
    #anchor = duplicate( twistJoints, po=True )[0]
    #anchor.rename( simpleName(jnt, '{0}Anchor') )
    
    for jnt in twistJoints:
        aimer = duplicate( jnt, po=True )[0]
        space = duplicate( jnt, po=True )[0]
        
        aimer.rename( simpleName(jnt, '{0}Aimer') )
        space.rename( simpleName(jnt, '{0}Space') )
        space.drawStyle.set(2)
        
        jnt.setParent( space )
        
        #hide(anchor, aimer)
        hide(aimer)
    
        constraint = orientConstraint( startSegment, aimer, space )
    
        constraint.interpType.set(2)  # Set to "shortest" because it will flip otherwise.
    
        aimConstraint( endSegment, aimer, wut='objectrotation', wuo=endSegment, mo=True,
                        u=twistLateralAxis, # identifyAxis(jnt, asVector=True),  # noqa e127
                        aimVector=[1, 0, 0], # identifyAxis(jnt, asVector=True),
                        wu=driverLateralAxis,
                    )
        
        baseRotAttr, endRotAttr = constraint.getWeightAliasList()
        
        driver = drive(control, simpleName(jnt, '{0}_Auto'), endRotAttr, minVal=0, maxVal=1, dv=defaultPower)
        core.math.opposite(driver) >> baseRotAttr
        
            
    #ctrl = control.build( trimName(twistDriver) + "Twist", controlSpec['main'], control.ROTATE)

    #ctrl.setParent(space)
    #ctrl.t.set( 0, 0, 0 )
    #ctrl.r.set( 0, 0, 0 )
    #lockScale( ctrl )
    #lockTrans( ctrl )
    #lockRot( ctrl )
    # # Unlock the twist axis
    #ctrl.attr( 'r' + identifyAxis(twist) ).unlock()
    #ctrl.attr( 'r' + identifyAxis(twist) ).setKeyable(True)
    
    # Drive the space's constraint
#    anchorAttr, autoAttr = orientConstraint( constraint, q=1, wal=1 )
#    drive( ctrl, 'AutoTwistPower', autoAttr, minVal=0, maxVal=1, dv=defaultPower )
#    core.math.opposite( ctrl.AutoTwistPower ) >> anchorAttr
#    ctrl.AutoTwistPower.set( defaultPower )
    
    #orientConstraint( ctrl, twist )
    
    #ctrl = nodeApi.RigController.convert(ctrl)
    #ctrl.container = container
    
    #return ctrl, #container


class EndOrient:
    TRUE_ZERO = 'True_Zero'             # Matches world but has true zero to return to bind
    JOINT = 'Joint'                     # Match the orient of the last joint (VERIFY this just mean it matches the joint, no true zero)
    TRUE_ZERO_FOOT = 'True_Zero_Foot'   # Same as TRUE_ZERO but only in xz plane
    WORLD = 'World'
    
    @classmethod
    def asChoices(cls):
        choices = collections.OrderedDict()
        choices[cls.TRUE_ZERO.replace('_', ' ')] = cls.TRUE_ZERO
        choices[cls.JOINT.replace('_', ' ')] = cls.JOINT
        choices[cls.TRUE_ZERO_FOOT.replace('_', ' ')] = cls.TRUE_ZERO_FOOT
        choices[cls.WORLD] = cls.WORLD
        return choices

#------------------------------------------------------------------------------
# Controls are actually made below here!
#------------------------------------------------------------------------------


@adds()
@defaultspec( {'shape': controller.control.sphere, 'color': 'orange 0.22', 'size': 10} )
def fkChain(start, end, translatable=False, groupName='', controlSpec={} ):
    '''
    Make an FK chain between the given joints.
    
    :param PyNode start: Start of joint chain
    :param PyNode end: End of chain
    :param bool translatable: Default=False
    :param dict controlSpec: Override default control details here.  Only has 'main'.
    
    ..  todo::
        I think I want to control spec housed elsewhere for easier accessibility.
    
    '''
    
    joints = getChain( start, end )
    if not joints:
        assert 'Could not make an chain between {0} and {1} because they are not in the same hierarchy'.format( start, end )
    
    container = parentGroup(start)
    
    container.setParent( lib.getNodes.mainGroup() )
    
    container.rename( trimName(start) + '_fkChain' )
    
    top = container
    
    leadOrient, leadPoint = None, None
    
    controls = []
    
    for j in joints:
        ctrl = controller.control.build(   trimName(j) + "_ctrl",
                                controlSpec['main'],
                                type=controller.control.TRANSLATE if translatable else controller.control.ROTATE )
        controls.append( ctrl )
        core.dagObj.matchTo( ctrl, j )
        space = core.dagObj.zero( ctrl )
        
        space.setParent( top )
        top = ctrl
        
        if not translatable:
            lockTrans( ctrl )
        lockScale( ctrl )
    
        orient, point = constrainTo( j, ctrl )
        
        if leadOrient:
            leadOrient >> orient
            leadPoint >> point
        else:
            leadOrient, leadPoint = orient, point
            
    #drive( start, CONTROL_ATTR_NAME, leadOrient )
    #drive( start, CONTROL_ATTR_NAME, leadPoint )
    
    #drive( controls[0], CONTROL_ATTR_NAME, start.attr(CONTROL_ATTR_NAME) )
    #controls[0].attr(CONTROL_ATTR_NAME).set(1)
    #controls[0].attr(CONTROL_ATTR_NAME).setKeyable(False)
    
    # Always unlock the translate of the lead rotate if more than 1 joint
    if start != end:
        controls[0].tx.unlock()
        controls[0].ty.unlock()
        controls[0].tz.unlock()
        controls[0].tx.setKeyable(True)
        controls[0].ty.setKeyable(True)
        controls[0].tz.setKeyable(True)
        
    controls[0] = nodeApi.RigController.convert(controls[0])
    controls[0].container = container
    for i, c in enumerate(controls[1:]):
        controls[0].subControl[str(i)] = c
        
    return controls[0], ConstraintResults(leadPoint, leadOrient )


@adds()
@defaultspec( {'shape': controller.control.sphere, 'color': 'orange 0.22', 'size': 10} )
def freeform(joints, translatable=False, groupName='', controlSpec={} ):
    '''
    Make an FK chain between the given joints.
    
    :param PyNode start: Start of joint chain
    :param PyNode end: End of chain
    :param bool translatable: Default=False
    :param dict controlSpec: Override default control details here.  Only has 'main'.
    
    ..  todo::
        I think I want to control spec housed elsewhere for easier accessibility.
    
    '''
    
    # Make a top level section for each lead joint in the subHierarchy
    #topLevel = [j for j in joints if j.getParent() not in joints]

    topContainer = group(n=simpleName(joints[0], '{0}_Freeform'), p=lib.getNodes.mainGroup(), em=True)
    
    #top = container
    #leadOrient, leadPoint = None, None
    
    controls = []
    
    done = {}
    for j in joints:
        done[j] = False

    for j in joints:

        ctrl = controller.control.build(   trimName(j) + "_ctrl",
                                controlSpec['main'],
                                type=controller.control.TRANSLATE if translatable else controller.control.ROTATE )
        controls.append( ctrl )
        core.dagObj.matchTo( ctrl, j )

        constrainTo( j, ctrl )

        space = core.dagObj.zero( ctrl )
        
        done[j] = (ctrl, space)
        #space.setParent( top )
        #top = ctrl
        
        if not translatable:
            lockTrans( ctrl )
        lockScale( ctrl )
    
        '''
        orient, point = constrainTo( j, ctrl )
        
        if leadOrient:
            leadOrient >> orient
            leadPoint >> point
        else:
            leadOrient, leadPoint = orient, point
        '''
    
    for jnt, (ctrl, space) in done.items():
        if jnt.getParent() in done:
            space.setParent( done[jnt.getParent()][0] )
        else:
            # containers = []
            # for top in topLevel:
            container = parentGroup(jnt)
            container.setParent(topContainer)
            container.rename( trimName(jnt) + '_fkChain' )

            space.setParent(container)
            # containers.append(container)

    # A leader must be choosen
    ctrl = nodeApi.RigController.convert(controls[0])
    for i, c in enumerate(controls[1:]):
        ctrl.subControl[str(i)] = c

    ctrl.container = topContainer

    return ctrl, None # ConstraintResults(leadPoint, leadOrient )


@adds()
@defaultspec( {'shape': controller.control.sphere, 'color': 'blue 0.22', 'size': 10} )
def ctrlGroup(parentJoint, point, rotation, name='Group', translatable=True, scalable=False, useTrueZero=False, groupName='', controlSpec={} ):
    '''
    Makes a control at the given point
    
    :param PyNode start: Start of joint chain
    :param bool translatable: Default=True
    :param dict controlSpec: Override default control details here.  Only has 'main'.
    '''
    
    # Can't use parentGroup() since target isn't a real joint.
    container = group( em=True, name='{0}_Proxy'.format(name) )
    
    if parentJoint:
        parentConstraint( parentJoint, container, mo=False )
    
    container.setParent( lib.getNodes.mainGroup() )
        
    ctrl = controller.control.build(   name + "_ctrl",
                            controlSpec['main'],
                            type=controller.control.TRANSLATE if translatable else controller.control.ROTATE )
    
    ctrl.t.set(point)

    if not useTrueZero:
        ctrl.r.set(rotation)
    
    core.dagObj.zero( ctrl ).setParent( container )

    if useTrueZero:
        ctrl.r.set(rotation)
        storeTrueZero(ctrl, rotation)
        
    if not translatable:
        lockTrans( ctrl )
        
    if not scalable:
        lockScale( ctrl )
                
    ctrl = nodeApi.RigController.convert(ctrl)
    ctrl.container = container
    
    leadOrient, leadPoint = None, None
    return ctrl, ConstraintResults(leadPoint, leadOrient)


@adds('stretch')
@defaultspec( {'shape': controller.control.box,    'color': 'blue 0.22', 'size': 10},
       middle={'shape': controller.control.sphere, 'color': 'blue 0.22', 'size': 10},
        start={'shape': controller.control.box,    'color': 'blue 0.22', 'size': 10},)
def splineNeck(start, end, name='', matchEndOrient=False, endOrient=EndOrient.TRUE_ZERO, curve=None, duplicateCurve=True, groupName='', controlSpec={} ):
    '''
    Makes a spline with a middle control point constrained between the endpoints.
    
    ..  todo::
        * Might want to make the end the main control to treat it more consistently
            with other IK, where the main is the end of the chain.
    '''
    if not name:
        name = simpleName(start)
        
    container = group(em=True, p=lib.getNodes.mainGroup(), n=name + "_controls")
    container.inheritsTransform.set(False)
    container.inheritsTransform.lock()
    
    controlChain = dupChain(start, end)
    controlChain[0].setParent(container)
    
    # If the chain is mirrored, we need to reorient to point down x so the
    # spline doesn't mess up when the main control rotates
    if controlChain[1].tx.get() < 0:
        # Despite aggresive zeroing of the source, the dup can still end up slightly
        # off zero so force it.
        for jnt in controlChain:
            jnt.r.set(0, 0, 0)
        joint( controlChain[0], e=True, oj='xyz', secondaryAxisOrient='yup', zso=True, ch=1)
        joint(controlChain[-1], e=True, oj='none')
    
    # Since the spline might shift the joints, make joints at the original pos
    # to constrain to.  This lets us make controls agnostically since we don't.
    # need to maintain offset
    offsetChain = dupChain(start, end, '{0}_offset')

    if curve:
        if duplicateCurve:
            crv = duplicate(curve)[0]
        else:
            crv = curve
        mainIk, _effector = ikHandle( sol='ikSplineSolver',
            sj=controlChain[0],  # noqa e128
            ee=controlChain[-1],
            ccv=False,
            pcv=False)
        crv.getShape().worldSpace[0] >> mainIk.inCurve

    else:
        mainIk, _effector, crv = ikHandle( sol='ikSplineSolver',
            sj=controlChain[0],  # noqa e128
            ee=controlChain[-1],
            ns=1)
        
    constraints = constrainAtoB( getChain(start, end)[:-1], offsetChain[:-1], mo=False)
    
    hide(mainIk, crv, controlChain[0])
    parent( mainIk, crv, container )
    
    startJnt = duplicate( start, po=True )[0]
    startJnt.setParent(w=True)
    endJnt = duplicate( end, po=True )[0]
    endJnt.setParent(w=True)
    
    midCtrl = controller.control.build( name + "_Mid", controlSpec['middle'], controller.control.SPLINE )
    lockScale(midCtrl)
    midPoint = pointOnCurve( crv, pr=0.5, p=True, top=True )
    midChain = findClosest(midPoint, getChain(start, end))
    core.dagObj.matchTo(midCtrl, midChain)
    midZero = core.dagObj.zero(midCtrl)
    midZero.t.set( midPoint )
    
    midJoint = joint(None)
    midJoint.setParent(midCtrl)
    midJoint.t.set(0, 0, 0)
    midJoint.r.set(0, 0, 0)
    midZero.setParent( container )
    
    # Setup mid controller spaces
    pointSpace = spaceLocator()
    pointSpace.rename('midPoint_{0}'.format(start))
    pointSpace.setParent(container)
    core.dagObj.moveTo(pointSpace, midCtrl)
    pointConstraint( startJnt, endJnt, pointSpace, mo=True )
    hide(pointSpace)
    space.add( midCtrl, pointSpace, spaceName='midPoint')
    
    childSpace = spaceLocator()
    childSpace.rename('midChild_{0}'.format(start))
    childSpace.setParent(container)
    core.dagObj.matchTo(childSpace, midCtrl)
    parentConstraint( startJnt, endJnt, childSpace, mo=True )
    hide(childSpace)
    space.add( midCtrl, childSpace, spaceName='midChild')
    
    pntRotSpace = spaceLocator()
    pntRotSpace.rename('midPntRot_{0}'.format(start))
    pntRotSpace.setParent(container)
    core.dagObj.matchTo(pntRotSpace, midCtrl)
    pointConstraint( startJnt, endJnt, pntRotSpace, mo=True )
    orientConstraint( startJnt, endJnt, pntRotSpace, mo=True )
    hide(pntRotSpace)
    space.add( midCtrl, pntRotSpace, spaceName='midPointRot')
    
    aimer = midAimer(startJnt, endJnt, midCtrl)
    aimer.setParent(container)
    hide(aimer)
    space.add( midCtrl, aimer, spaceName='mid_aim')
    
    # Build Start and end controllers
    
    skinCluster(startJnt, endJnt, midJoint, crv, tsb=True)
    
    startCtrl = controller.control.build( name + '_Start', controlSpec['start'], controller.control.SPLINE )
    lockScale(startCtrl)
    core.dagObj.matchTo( startCtrl, startJnt )
    startSpace = core.dagObj.zero(startCtrl)
    startSpace.setParent(container)
    
    endCtrl = controller.control.build( name + '_End', controlSpec['main'], controller.control.SPLINE )
    lockScale(endCtrl)
    
    #core.dagObj.moveTo( endCtrl, end )
    #core.dagObj.zero( endCtrl ).setParent( container )
    
    """
    ORIGINAL matchEndOrient code
    if not matchEndOrient:
        core.dagObj.matchTo( endCtrl, endJnt )
        print('WHAT THE FUCK?')
    else:
        print( 'JUST MOVING' )
        core.dagObj.moveTo( endCtrl, endJnt )
    
    core.dagObj.zero(endCtrl).setParent(container)
    
    if matchEndOrient:
        rot = determineClosestWorldOrient(end)
        endCtrl.r.set( rot )
        storeTrueZero(endCtrl, rot)
    """
    
    # Begin new endOrient enum code (replacing matchEndOrient)
    # matchEndOrient=False == TRUE_ZERO
    # matchEndOrient=True  == JOINT
    if endOrient == EndOrient.WORLD:
        core.dagObj.moveTo( endCtrl, endJnt )
        
    elif endOrient == EndOrient.JOINT:
        core.dagObj.matchTo( endCtrl, endJnt )
        
    elif endOrient == EndOrient.TRUE_ZERO:
        core.dagObj.moveTo( endCtrl, endJnt )
    
    core.dagObj.zero(endCtrl).setParent(container)
    
    if endOrient == EndOrient.TRUE_ZERO:
        rot = determineClosestWorldOrient(end)
        endCtrl.r.set( rot )
        storeTrueZero(endCtrl, rot)
    
    # End new endOrient enum code
    
    makeStretchySpline( endCtrl, mainIk )
    
    # Constraint to endJnt, which has the same orientation as end instead of endCtrl
    endJnt.setParent( endCtrl )
    endConstraints = constrainAtoB( [end], [endJnt] )
    
    driveConstraints( constraints, endConstraints )
    hide( startJnt, endJnt, midJoint )
    
    space.addWorld(endCtrl)
    space.add( endCtrl, start.getParent(), 'parent' )
    space.add( endCtrl, startCtrl, 'start' )
    
    space.add( startCtrl, start.getParent(), 'parent' )
    
    startJnt.setParent( startCtrl )
    
    orientConstraint( endCtrl, controlChain[-1], mo=True )
    
#    ctrls = addControlsToCurve('Blah', crv)

#    startCtrl.setParent( ctrls[0] )
#    endCtrl.setParent( ctrls[3] )
    
#    parentConstraint( ctrls[0], midCtrl, ctrls[1], mo=True )
#    parentConstraint( ctrls[3], midCtrl, ctrls[2], mo=True )
    
    #hide( ctrls[1:3] )
    
    
    # Setup matchers for easy ik switching later
    endMatch = createMatcher(endCtrl, end)
    endMatch.setParent(container)
    
    startMatch = createMatcher(startCtrl, start)
    startMatch.setParent(container)
    
    distances = {}
    jointChain = getChain(start, end)
        
    for j in jointChain:
        distances[ core.dagObj.distanceBetween(j, midCtrl) ] = j
    
    for dist, j in sorted(distances.items()):
        # Make a matcher here
        midMatch = createMatcher(midCtrl, j)
        midMatch.setParent(container)
        break
    
    # Setup the endControl as the leader
    
    endCtrl = nodeApi.RigController.convert(endCtrl)
    endCtrl.container = container
    endCtrl.subControl['mid'] = midCtrl
    endCtrl.subControl['start'] = startCtrl

    # Since the chain might have reversed, use the control chain for the twist axes.
    advancedTwist(controlChain[0], controlChain[1], startCtrl, endCtrl, mainIk)
    # Since adding the advanced twist can slightly alter things (sometimes),
    # put the offset joints in as the last step
    for ctrl, offset in zip(controlChain, offsetChain):
        offset.setParent(ctrl)
    
    return endCtrl, constraints


@adds()  # ??? adds
def splineEndTwist(start, end, name):

    container = group(em=True, p=getIkGroup(), n=name + "_controls")
    container.inheritsTransform.set(False)
    container.inheritsTransform.lock()
    
    mainIk, _effector, crv = ikHandle( sol='ikSplineSolver',
        sj=start,
        ee=end,
        ns=1)
        
    controls = addControlsToCurve(crv)
    
    space.addWorld( controls[0], mode=space.Mode.ROTATE )
    space.add( controls[0], start.getParent(), mode=space.Mode.ROTATE )
    
    for prev, ctrl in zip( controls[:-1], controls[1:] ):
        space.add( ctrl, prev )
        space.addWorld(ctrl)


@adds('AutoTwistPower')
@defaultspec( {'shape': controller.control.disc, 'color': 'blue 0.22', 'size': 5, 'align': 'x'} )
def twist(twist, twistDriver, twistLateralAxis=[0, 1, 0], driverLateralAxis=[0, 1, 0], defaultPower=0.5, controlSpec={}):
    '''
    Twist bone's aim axis = the lateral axis
    Twist Up axis = points to the target (wrist)
    
    World up = object rotation
    up obj = target (wrist)
    up axis = I think this is the target's lateral axis
    
    ..  todo::
        I'm not sure, but it look like a "_L" is sneaking into the name somewhere
    '''
    
    container = parentGroup(twist)
    container.setParent( lib.getNodes.mainGroup() )
    container.rename( trimName(twist) + '_twist' )
    
    anchor = duplicate( twist, po=True )[0]
    aimer = duplicate( twist, po=True )[0]
    space = duplicate( twist, po=True )[0]
    anchor.rename( simpleName(twist, '{0}Anchor') )
    aimer.rename( simpleName(twist, '{0}Aimer') )
    space.rename( simpleName(twist, '{0}Space') )
    space.drawStyle.set(2)
    
    hide(anchor, aimer)
    parent( anchor, aimer, space, container )
    
    constraint = orientConstraint( anchor, aimer, space )
    constraint.interpType.set(2)  # Set to "shortest" because it will flip otherwise.
    
    aimConstraint( twistDriver, aimer, wut='objectrotation', wuo=twistDriver, mo=True,
                    u=identifyAxis(twist, asVector=True),  # noqa e127
                    aimVector=twistLateralAxis,
                    wu=driverLateralAxis,
                )
    
    ctrl = controller.control.build( trimName(twistDriver) + "Twist", controlSpec['main'], controller.control.ROTATE)

    ctrl.setParent(space)
    ctrl.t.set( 0, 0, 0 )
    ctrl.r.set( 0, 0, 0 )
    lockScale( ctrl )
    lockTrans( ctrl )
    lockRot( ctrl )
    # Unlock the twist axis
    ctrl.attr( 'r' + identifyAxis(twist) ).unlock()
    ctrl.attr( 'r' + identifyAxis(twist) ).setKeyable(True)
    
    # Drive the space's constraint
    anchorAttr, autoAttr = orientConstraint( constraint, q=1, wal=1 )
    drive( ctrl, 'AutoTwistPower', autoAttr, minVal=0, maxVal=1, dv=defaultPower )
    core.math.opposite( ctrl.AutoTwistPower ) >> anchorAttr
    ctrl.AutoTwistPower.set( defaultPower )
    
    orientConstraint( ctrl, twist )
    
    ctrl = nodeApi.RigController.convert(ctrl)
    ctrl.container = container
    
    return ctrl, container


@adds('stretch')
@defaultspec( {'shape': controller.control.box,    'color': 'orange 0.22', 'size': 10 },
     shoulder={'shape': controller.control.box,    'color': 'orange 0.22', 'size': 10 },  # noqa e128
         neck={'shape': controller.control.pin,    'color': 'orange 0.22', 'size': 10, 'align': 'z'} )
def splineChestFourJoint(start, end, name='Chest', groupName='', controlSpec={}):
    '''
    Simplified version of splineChest but with only 3 joints and no mid section.
    '''

    if not name:
        name = trimName(start) + '_Spline'
    container = group( n=name + '_splineChest' )
    container.setParent( lib.getNodes.mainGroup() )
    if start.getParent():
        parentConstraint(start.getParent(), container, mo=True)
   
    chain = getChain( start, end )
        
    controlChain = dupChain(start, end)
    controlChain[0].setParent(container)
    hide(controlChain[0])
    
    constraints = constrainAtoB( chain, controlChain )
    
    # Chest controller
    chestCtrl = controller.control.build( name, controlSpec['main'], type=controller.control.IK )
    chestCtrl.setParent(container)
    core.dagObj.moveTo( chestCtrl, chain[1] )
    core.dagObj.zero(chestCtrl)
    trueZeroSetup(chain[1], chestCtrl)
    lockScale(chestCtrl)

    space.add( chestCtrl, start.getParent(), 'local' )
    space.add( chestCtrl, start.getParent(), 'local_posOnly', mode=space.Mode.TRANSLATE )
    space.addWorld( chestCtrl )
    space.addTrueWorld( chestCtrl )
    
    # Main Ik
    mainIk = ikHandle( sol='ikSCsolver', sj=controlChain[0], ee=controlChain[1] )[0]
    hide(mainIk)
    mainIk.setParent( chestCtrl )
    
    # Allow the chain to not stretch
    orientTarget = duplicate( chain[1], po=True )[0]
    orientTarget.setParent(chestCtrl)
    lockTrans(lockScale(orientTarget))
    orientConstraint( orientTarget, controlChain[1] )
    hide(orientTarget)
    
    lockRot(mainIk)
    lockTrans(mainIk)
    lockScale(mainIk)
    
    # Shoulder controller
    chestFollow = group(em=True, n='chestFollow', p=container)
    #parentConstraint(controlChain[1], chestFollow)
    parentConstraint(chestCtrl, chestFollow)
    
    shoulderCtrl = controller.control.build( name + '_Shoulder', controlSpec['shoulder'], type=controller.control.IK )
    core.dagObj.matchTo(shoulderCtrl, controlChain[-1])
    shoulderCtrl.setParent(chestFollow)
    core.dagObj.zero(shoulderCtrl)
    lockTrans(lockScale(shoulderCtrl))
    
    #lower = ikHandle( sol='ikSCsolver', sj=controlChain[-3], ee=controlChain[-2], n='lowerChest')[0]
    #upper = ikHandle( sol='ikSCsolver', sj=controlChain[-2], ee=controlChain[-1], n='upperChest')[0]
    #lower.setParent(shoulderCtrl)
    #upper.setParent(shoulderCtrl)
    
    chestBaseAim = group(em=True, n='chestBaseAim', p=shoulderCtrl)
    core.dagObj.moveTo(chestBaseAim, controlChain[-2])
    #parentConstraint(shoulderCtrl, chestBaseAim, sr=list('xyz'), mo=True)
    pointConstraint(chestBaseAim, controlChain[-2])
    
    #
    # Neck controller
    neckCtrl = controller.control.build( name + '_Neck', controlSpec['neck'], type=controller.control.IK )
    core.dagObj.matchTo(neckCtrl, controlChain[-1])
    core.dagObj.zero(neckCtrl).setParent(shoulderCtrl)
    orientConstraint(neckCtrl, controlChain[-1], mo=True)
    pointConstraint(neckCtrl, controlChain[-1])
    
    aimConstraint(chestBaseAim, orientTarget, aim=[1, 0, 0], u=[0, 1, 0], wut='objectrotation', wuo=chestCtrl, mo=True)
    aimConstraint(shoulderCtrl, controlChain[-2], aim=[1, 0, 0], u=[0, 1, 0], wut='objectrotation', wuo=chestCtrl, mo=True)
    
    makeStretchySpline(chestCtrl, mainIk)
    # It's easier to lock and hide to ignore this than not add the length attr at all.
    chestCtrl.length.set(k=False)
    chestCtrl.length.lock()
    
    # Register all the parts of the control for easy identification at other times.
    chestCtrl = nodeApi.RigController.convert(chestCtrl)
    chestCtrl.container = container
    chestCtrl.subControl['neck'] = neckCtrl
    chestCtrl.subControl['shoulders'] = shoulderCtrl
    
    return chestCtrl, constraints


@adds('stretch')
@defaultspec( {'shape': controller.control.box,    'color': 'orange 0.22', 'size': 10 },
         neck={'shape': controller.control.pin,    'color': 'orange 0.22', 'size': 10, 'align': 'z'} )  # noqa e128
def splineChestThreeJoint(start, end, name='Chest', groupName='', controlSpec={}):
    '''
    Simplified version of splineChest but with only 3 joints and no mid section.
    '''

    if not name:
        name = trimName(start) + '_Spline'
    container = group( n=name + '_grpX' )
    container.setParent( lib.getNodes.mainGroup() )
    if start.getParent():
        parentConstraint(start.getParent(), container, mo=True)
   
    chain = getChain( start, end )
        
    controlChain = dupChain(start, end)
    controlChain[0].setParent(container)
    hide(controlChain[0])
    
    constraints = constrainAtoB( chain, controlChain )
    
    # Chest controller
    chestCtrl = controller.control.build( name, controlSpec['main'], type=controller.control.IK )
    chestCtrl.setParent(container)
    core.dagObj.moveTo( chestCtrl, chain[1] )
    core.dagObj.zero(chestCtrl)
    trueZeroSetup(chain[1], chestCtrl)
    lockScale(chestCtrl)

    space.add( chestCtrl, start.getParent(), 'local' )
    space.add( chestCtrl, start.getParent(), 'local_posOnly', mode=space.Mode.TRANSLATE )
    space.addWorld( chestCtrl )
    space.addTrueWorld( chestCtrl )

    # Main Ik
    mainIk = ikHandle( sol='ikSCsolver', sj=controlChain[0], ee=controlChain[1] )[0]
    hide(mainIk)
    mainIk.setParent( chestCtrl )
    
    # Allow the chain to not stretch
    orientTarget = duplicate( chain[1], po=True )[0]
    orientTarget.setParent(chestCtrl)
    lockTrans(lockRot(lockScale(orientTarget)))
    orientConstraint( orientTarget, controlChain[1] )
    hide(orientTarget)
    
    lockRot(mainIk)
    lockTrans(mainIk)
    lockScale(mainIk)
    
    # Neck
    chestFollow = group(em=True, n='chestFollow', p=container)
    neckCtrl = controller.control.build( name + '_Neck', controlSpec['neck'], type=controller.control.IK )
    core.dagObj.matchTo(neckCtrl, controlChain[-1])
    core.dagObj.zero(neckCtrl).setParent(chestFollow)
    parentConstraint(controlChain[1], chestFollow, mo=True)
    parentConstraint(neckCtrl, controlChain[-1], mo=True)
    lockTrans(lockScale(neckCtrl))
    
    
    makeStretchySpline(chestCtrl, mainIk)
    # It's easier to lock and hide to ignore this than not add the length attr at all.
    chestCtrl.length.set(k=False)
    chestCtrl.length.lock()
    
    # Register all the parts of the control for easy identification at other times.
    chestCtrl = nodeApi.RigController.convert(chestCtrl)
    chestCtrl.container = container
    chestCtrl.subControl['neck'] = neckCtrl
    
    return chestCtrl, constraints


@adds('stretch')
@defaultspec( {'shape': controller.control.box,    'color': 'orange 0.22', 'size': 10 },
       middle={'shape': controller.control.sphere, 'color': 'green  0.22', 'size': 7  },   # noqa e128
          end={'shape': controller.control.box,    'color': 'orange 0.22', 'size': 10 },
         neck={'shape': controller.control.pin,    'color': 'orange 0.22', 'size': 10, 'align': 'z'},)
def splineChest(start, end, name='Chest', numChestJoints=3, useTrueZero=True, groupName='', controlSpec={}):
    '''
    Makes a spline which considers the last 3 joints the "chest/neck".  A chest
    mass is made, with the neck providing a small amount of offset mainly
    affecting that mass.  A mid control is made to adjust the stomach.
    
    ..  todo::
        * Add support for groupName
        * Finish adding ParamInfo support for strings so a specific name can be given
        * Make the number of stomach joints variable but the chest always is always
            3rd from the top
    '''
    srcChain = getChain( start, end )
    
    chain = dupChain( start, end, '{0}_spline' )
    
    chestBase = chain[-numChestJoints]
    chestIndex = len(chain) - numChestJoints
        
    midPoint = chain[1]  # &&& NEED TO FIGURE OUT REAL MID POINT
        
    container = group(em=True, p=lib.getNodes.mainGroup(), n=name + "_controls")
    container.inheritsTransform.set(False)
    container.inheritsTransform.lock()
    chain[0].setParent(container)
    
    mainIk, _effector, crv = ikHandle( sol='ikSplineSolver',
        sj=chain[0],
        ee=chain[-1],
        ns=3,
        simplifyCurve=False)
    
    crvShape = crv.getShape()
    crvShape.overrideEnabled.set(True)
    crvShape.overrideDisplayType.set(2)
    
    parent( mainIk, crv, container )
        
    # -- Base --
    base = joint(None, n='Base')
    core.dagObj.moveTo(base, chain[0])
    base.setParent( container )
    parentConstraint( start.getParent(), base, mo=True)
    hide(base)
        
    # -- Chest control --
    chestCtrl = controller.control.build( name + '_main', controlSpec['main'], controller.control.SPLINE )
    chestCtrl.setParent(container)
    makeStretchySpline( chestCtrl, mainIk )
    chestCtrl.stretch.set(1)
    chestCtrl.stretch.lock()
    chestCtrl.stretch.setKeyable(False)
    lockScale(chestCtrl)
    space.add( chestCtrl, start.getParent(), 'local' )
    space.add( chestCtrl, start.getParent(), 'local_posOnly', mode=space.Mode.TRANSLATE )
    space.addWorld( chestCtrl )
    space.addTrueWorld( chestCtrl )

    # Put pivot point at the bottom
    chestCtrl.ty.set( chestCtrl.boundingBox()[1][1] )
    
    lib.sharedShape.remove(chestCtrl)
    chestCtrl.setPivots( [0, 0, 0], worldSpace=True )
    makeIdentity( chestCtrl, a=True, t=True )
    lib.sharedShape.use(chestCtrl)
    
    move( chestCtrl, xform(chestBase, q=True, ws=True, t=True), rpr=True )
    chestZero = core.dagObj.zero(chestCtrl)
    
    if useTrueZero:
        rot = determineClosestWorldOrient(chestBase)
        
        storeTrueZero(chestCtrl, rot)
        core.dagObj.rezero( chestCtrl )  # Not sure why this is needed but otherwise the translate isn't zeroed
        chestCtrl.r.set( rot )
    
    chest = joint(None, n='Chest')
    chest.setParent( chestCtrl )
    core.dagObj.moveTo(chest, chestBase)
    lockScale(lockRot(lockTrans(chest)))
    hide(chest)

    chestMatcher = createMatcher(chestCtrl, srcChain[chestIndex])
    chestMatcher.setParent(container)
    
    # -- Mid --
    midCtrl = controller.control.build( name + '_mid', controlSpec['middle'], controller.control.SPLINE )
    core.dagObj.matchTo( midCtrl, midPoint )
    lockScale(midCtrl)
    midCtrl.setParent( container )
    
    mid = joint(None, n='Mid')
    core.dagObj.moveTo( mid, midPoint )
    mid.setParent( midCtrl )
    lockScale(lockRot(lockTrans(mid)))
    hide(mid)
    
    # Mid control's rotation aims at the chest
    core.dagObj.zero(midCtrl)
    
    aimer = midAimer(base, chestCtrl, midCtrl)
    aimer.setParent(container)
    hide(aimer)
    '''
    aimer = group(em=True, name='aimer')
    aimer.setParent(container)
    #aimer = polyCone(axis=[1, 0, 0])[0]
    core.dagObj.moveTo(aimer, midCtrl)
    pointConstraint(chestCtrl, base, aimer, mo=True)
    
    # Determine which axis of the chest control is closest to the midControl's Y axis.
    chestMatrix = xform(chestCtrl, q=True, ws=True, m=True)
    midMatrix = xform(midCtrl, q=True, ws=True, m=True)
    midCtrlYUp = dt.Vector(midMatrix[4:7])
    
    choices = [
        (chestMatrix[4:7], [0, 1, 0]),
        ([-x for x in chestMatrix[4:7]], [0, -1, 0]),
        (chestMatrix[8:11], [0, 0, -1]),
        ([-x for x in chestMatrix[8:11]], [0, 0, 1]),
    ]
    
    low = midCtrlYUp.angle(choices[0][0])
    axis = choices[0][1]
    for vector, destAxis in choices[1:]:
        if midCtrlYUp.angle(vector) < low:
            low = midCtrlYUp.angle(vector)
            axis = destAxis
    
    aimConstraint( chestCtrl, aimer, wut='objectrotation', aim=[1, 0, 0], wuo=chestCtrl, upVector=[0, 1, 0], wu=axis, mo=False)
    '''
    
    space.add(midCtrl, aimer, spaceName='default')

    # -- Shoulders --
    if numChestJoints > 2: # The shoulder control is skipped if there aren't enough joints
        shoulderCtrl = controller.control.build( name + '_shoulders', controlSpec['end'], controller.control.SPLINE )
        core.dagObj.matchTo( shoulderCtrl, srcChain[-2])  # We want to use the penultimate joint orientation
        core.dagObj.moveTo( shoulderCtrl, end)
        controller.control.scale( shoulderCtrl, x=0.15 )
        shoulderZero = core.dagObj.zero(shoulderCtrl)
        shoulderZero.setParent(chestCtrl)
        lockScale(lockTrans(shoulderCtrl))
    
        neck = joint(None, n='Neck')
        neck.setParent( shoulderCtrl )
        core.dagObj.moveTo( neck, end )
        lockScale(lockRot(lockTrans(neck)))
        hide(neck)
    
    # -- Neck --
    neckCtrl = controller.control.build( name + '_neck', controlSpec['neck'], controller.control.ROTATE )
    core.dagObj.matchTo( neckCtrl, end)
    if numChestJoints > 2: # The shoulder control is skipped if there aren't enough joints
        core.dagObj.zero(neckCtrl).setParent( shoulderCtrl )
        lockScale(lockTrans(neckCtrl))
        space.add( neckCtrl, srcChain[-2], 'chest' )
        
    else:
        core.dagObj.zero(neckCtrl).setParent( chestCtrl )
        lockScale(lockTrans(neckCtrl))
        space.add( neckCtrl, chestCtrl, 'chest' )
        
    space.addWorld(neckCtrl)
    
    # Constrain to spline proxy, up to the chest...
    constraints = []
    for src, dest in zip( chain, srcChain )[:chestIndex]:
        constraints.append( core.constraints.pointConst( src, dest ) )
        constraints.append( core.constraints.orientConst( src, dest ) )
    
    # ... including the chest
    src = chain[chestIndex]
    dest = srcChain[chestIndex]
    if numChestJoints > 2: # The shoulder control is skipped if there aren't enough joints
        constraints.append( core.constraints.pointConst( src, dest ) )
        constraints.append( core.constraints.orientConst( src, dest ) )
    # ... not including the chest
    else:
        chestProxy = duplicate(src, po=True)[0]
        chestProxy.setParent(chestCtrl)
        constraints.append( core.constraints.pointConst( chestProxy, dest ) )
        constraints.append( core.constraints.orientConst( chestProxy, dest ) )
        hide(chestProxy)
        
    constraints.append( core.constraints.pointConst( neckCtrl, srcChain[-1] ) )
    constraints.append( core.constraints.orientConst( neckCtrl, srcChain[-1] ) )
    
    if numChestJoints > 2: # The shoulder control is skipped if there aren't enough joints
        # Make a proxy since we can't constrain with maintainOffset=True if we're making fk too.
        proxy = duplicate(srcChain[-2], po=True)[0]
        proxy.setParent(neck)
        lockTrans(lockRot(lockScale(proxy)))
        
        constraints.append( core.constraints.pointConst( proxy, srcChain[-2] ) )
        constraints.append( core.constraints.orientConst( proxy, srcChain[-2] ) )
    
    hide(chain, mainIk)
    
    # Bind joints to the curve
    if numChestJoints > 2: # The shoulder control is skipped if there aren't enough joints
        skinCluster( crv, base, mid, chest, neck, tsb=True )
    else:
        skinCluster( crv, base, mid, chest, tsb=True )
    
    chestCtrl = nodeApi.RigController.convert(chestCtrl)
    chestCtrl.container = container
    chestCtrl.subControl['mid'] = midCtrl
    if numChestJoints > 2: # The shoulder control is skipped if there aren't enough joints
        chestCtrl.subControl['offset'] = shoulderCtrl
    chestCtrl.subControl['neck'] = neckCtrl
    
    # Setup advanced twist
    startAxis = duplicate( start, po=True )[0]
    startAxis.rename( 'startAxis' )
    startAxis.setParent( base )
    lockTrans(lockRot(lockScale(startAxis)))
    
    endAxis = duplicate( start, po=True )[0]
    endAxis.rename( 'endAxis' )
    endAxis.setParent( chestCtrl )
    endAxis.t.set(0, 0, 0)
    lockTrans(lockRot(lockScale(endAxis)))
    
    hide(startAxis, endAxis)
    
    mainIk.dTwistControlEnable.set(1)
    mainIk.dWorldUpType.set(4)
    startAxis.worldMatrix[0] >> mainIk.dWorldUpMatrix
    endAxis.worldMatrix[0] >> mainIk.dWorldUpMatrixEnd
    
    hide(startAxis, endAxis)
    
    return chestCtrl, constraints
    '''
    # For some reason, direct binding doesn't work out, it throws cycle errors
    # but it would be good to get it working like this for consistency.
    lib.weights.set( crv,
        [   [(base.name(), 1.0)],
            [(mid.name(), 0.05), (base.name(), 0.95)],
            [(mid.name(), 1.0) ],
            [(chest.name(), 1.0) ],
            [(chest.name(), 0.55), (end.name(), 0.45)],
            [(neck.name(), 1.0)],
            [(neck.name(), 1.0)] ] )
    '''


@adds('stretch', 'length')
@defaultspec( {'shape': controller.control.box,    'size': 10, 'color': 'green 0.22' },  # noqa e231
           pv={'shape': controller.control.sphere, 'size': 5,  'color': 'green 0.22' },
       socket={'shape': controller.control.sphere, 'size': 5,  'color': 'green 0.22', 'visGroup': 'socket' } )
def ikChain(start, end, pvLen=None, stretchDefault=1, endOrientType=EndOrient.TRUE_ZERO, name='', groupName='', controlSpec={}):
    '''
    
    :param int pvLen: How far from the center joint to be, defaults to half the length of the chain.
    ..  todo::
        * Have fk build as rotate only if not stretchy
        
    '''
    
    #controlChain = getChain( start, end )
    chain = getChain( start, end )
    
    #if not controlChain:
    #   assert 'Could not make an chain between {0} and {1} because they are not in the same hierarchy'.format( start, end )
    
    controlChain = dupChain(start, end)
    
    out = calcOutVector(controlChain[0], controlChain[1], controlChain[-1])
    
    constraints = constrainAtoB( chain, controlChain )
    '''
    for cc, orig in zip( controlChain, chain ):
        pointConstraint( cc, orig )
        orientConstraint( cc, orig )
    '''
    
    #duplicate(controlChain)
    
    # Main Ik
    mainIk = ikHandle( sol='ikRPsolver', sj=controlChain[0], ee=controlChain[-1] )[0]
    # NOT using Spring because it acts odd.  If the pelvis turns, the poleVectors follow it.
    # Make as RP first so the ik doesn't flip around
    #PyNode('ikSpringSolver').message >> mainIk.ikSolver
    
    hide(mainIk)
    
    if not name:
        name = trimName(start) + '_Ik'
    
    ctrl = controller.control.build( name, controlSpec['main'], type=controller.control.IK )
    
    container = group( n=name + '_grp' )
    container.setParent( lib.getNodes.mainGroup() )
    
    core.dagObj.moveTo( ctrl, end )
    core.dagObj.zero( ctrl ).setParent( container )
    
    if endOrientType == EndOrient.TRUE_ZERO:
        trueZeroSetup(end, ctrl)
    elif endOrientType == EndOrient.TRUE_ZERO_FOOT:
        trueZeroFloorPlane(end, ctrl)
    elif endOrientType == EndOrient.JOINT:
        core.dagObj.matchTo(ctrl, end)
        
        ctrl.rx.set( shortestAxis(ctrl.rx.get()) )
        ctrl.ry.set( shortestAxis(ctrl.ry.get()) )
        ctrl.rz.set( shortestAxis(ctrl.rz.get()) )
        
        core.dagObj.zero(ctrl)
    elif endOrientType == EndOrient.WORLD:
        # Do nothing, it's built world oriented
        pass
    
    lockScale(ctrl)
    
    mainIk.setParent( ctrl )
    orientTarget = duplicate( end, po=True )[0]
    orientTarget.setParent(ctrl)
    lockTrans(lockRot(lockScale(orientTarget)))
    orientConstraint( orientTarget, controlChain[-1] )
    hide(orientTarget)
    
    lockRot(mainIk)
    lockTrans(mainIk)
    lockScale(mainIk)
        
    # PoleVector
    if not pvLen or pvLen < 0:
        pvLen = chainLength(controlChain) * 0.5
    pvPos = out * pvLen + dt.Vector(xform(controlChain[1], q=True, ws=True, t=True))
    pvCtrl = controller.control.build( name + '_pv', controlSpec['pv'], type=controller.control.POLEVECTOR )
    
    lockScale(lockRot(pvCtrl))
    xform(pvCtrl, ws=True, t=pvPos)
    controller.control.line(pvCtrl, controlChain[1] )
    poleVectorConstraint( pvCtrl, mainIk )
    core.dagObj.zero(pvCtrl).setParent(container)
    
    # Socket offset control
    socketOffset = controller.control.build( name + '_socket', controlSpec['socket'], type=controller.control.TRANSLATE )
    socketContainer = parentGroup( start )
    socketContainer.setParent( container )
    
    core.dagObj.moveTo( socketOffset, start )
    core.dagObj.zero( socketOffset ).setParent( socketContainer )
    lockRot( socketOffset )
    lockScale( socketOffset )
    pointConstraint( socketOffset, controlChain[0] )
    
    # Reuse the socketOffset container for the controlling chain
    controlChain[0].setParent( socketContainer )
    hide( controlChain[0] )
    
    ''' Currently unable to get this to update, maybe order of operations needs to be enforced?
    # Add switch to reverse the direction of the bend
    reverseAngle = controlChain[1].jointOrient.get()[1] * -1.1
    ctrl.addAttr( 'reverse', at='short', min=0, max=1, dv=0, k=True )
    preferredAngle = core.math.condition( ctrl.reverse, '=', 0, 0, reverseAngle )
    twist = core.math.condition( ctrl.reverse, '=', 0, 0, -180)
    preferredAngle >> controlChain[1].preferredAngleY
    twist >> mainIk.twist
    core.math.condition( mainIk.twist, '!=', 0, 0, 1 ) >> mainIk.twistType # Force updating??
    '''

    makeStretchyNonSpline(ctrl, mainIk, stretchDefault)
    
    # Register all the parts of the control for easy identification at other times.
    ctrl = nodeApi.RigController.convert(ctrl)
    ctrl.container = container
    ctrl.subControl['pv'] = pvCtrl
    ctrl.subControl['socket'] = socketOffset

    # Add default spaces
    space.addWorld( pvCtrl )
    #space.add( pvCtrl, ctrl, spaceName=shortName(ctrl, '{0}_posOnly') )
    #space.add( pvCtrl, ctrl, spaceName=shortName(ctrl, '{0}_posOnly'), mode=space.TRANSLATE)
    space.add( pvCtrl, ctrl )
    space.add( pvCtrl, ctrl, mode=space.Mode.TRANSLATE)
    
    return ctrl, constraints


@adds('stretch', 'length')
@defaultspec( {'shape': controller.control.box,    'size': 10, 'color': 'green 0.22' },  # noqa e231
           pv={'shape': controller.control.sphere, 'size': 5,  'color': 'green 0.22' },
       socket={'shape': controller.control.sphere, 'size': 5,  'color': 'green 0.22', 'visGroup': 'socket' } )
def ikChain2(start, end, pvLen=None, stretchDefault=1, endOrientType=EndOrient.TRUE_ZERO, twists={}, name='', groupName='', controlSpec={}):
    '''
    
    :param int pvLen: How far from the center joint to be, defaults to half the length of the chain.
    ..  todo::
        * Have fk build as rotate only if not stretchy
    
    :param dict twists: Indicates how many twists each section has, ex {1: 2} means
        joint[1] has 2 twists, which means a 3 joint arm chain becomes
        shoulder, elbow, twist1, twist2, wrist

    '''
    
    chain = getChain( start, end )
    
    # Simplify the names
    controlChain = dupChain(start, end)
    for j, orig in zip(controlChain, chain):
        j.rename( trimName(orig) )
    
    mainJointCount = len(chain) - sum( twists.values() )
    
    # Take the linear chain and figure out what are the "main ik", and which
    # are the twist joints.  Also parent the mainArmature as a solo chain for ik application.
    mainArmature = []
    subTwists = {}
    cur = 0
    for i in range(mainJointCount):
        mainArmature.append( controlChain[cur] )
        
        if len(mainArmature) > 1:  # Need to reparent so the 'pivot' joints are independent of the twists
        
            if mainArmature[-1].getParent() != mainArmature[-2]: # ... unless this section has no twists and is already parented.
                mainArmature[-1].setParent(mainArmature[-2])
        
        cur += 1
        if i in twists:
            subTwists[ mainArmature[-1] ] = []
            
            for ti in range(twists[i]):
                subTwists[ mainArmature[-1] ].append( controlChain[cur] )
                controlChain[cur].setParent(w=True)  # This ends up being temporary so the ik is applied properly
                cur += 1

    # actual ik node
    mainIk = ikHandle( sol='ikRPsolver', sj=mainArmature[0], ee=mainArmature[-1] )[0]
    # NOT using Spring because it acts odd.  If the pelvis turns, the poleVectors follow it.
    # Make as RP first so the ik doesn't flip around
    #PyNode('ikSpringSolver').message >> mainIk.ikSolver


    # Build the main ik control
    
    hide(mainIk)
    hide(controlChain)
    
    if not name:
        name = trimName(start) + '_Ik'
    
    ctrl = controller.control.build( name, controlSpec['main'], type=controller.control.IK )
    
    container = group( n=name + '_grp' )
    container.setParent( lib.getNodes.mainGroup() )
    
    core.dagObj.moveTo( ctrl, end )
    core.dagObj.zero( ctrl ).setParent( container )

    # Orient the main ik control
    if endOrientType == EndOrient.TRUE_ZERO:
        trueZeroSetup(end, ctrl)
    elif endOrientType == EndOrient.TRUE_ZERO_FOOT:
        trueZeroFloorPlane(end, ctrl)
    elif endOrientType == EndOrient.JOINT:
        core.dagObj.matchTo(ctrl, end)
        
        ctrl.rx.set( shortestAxis(ctrl.rx.get()) )
        ctrl.ry.set( shortestAxis(ctrl.ry.get()) )
        ctrl.rz.set( shortestAxis(ctrl.rz.get()) )
        
        core.dagObj.zero(ctrl)
    elif endOrientType == EndOrient.WORLD:
        # Do nothing, it's built world oriented
        pass
    
    lockScale(ctrl)
    
    mainIk.setParent( ctrl )
    
    # I think orientTarget is for matching fk to ik
    orientTarget = duplicate( end, po=True )[0]
    orientTarget.setParent(ctrl)
    lockTrans(lockRot(lockScale(orientTarget)))
    orientConstraint( orientTarget, mainArmature[-1] )
    hide(orientTarget)
    
    lockRot(mainIk)
    lockTrans(mainIk)
    lockScale(mainIk)


    attr, jointLenMultiplier = makeStretchyNonSpline(ctrl, mainIk, stretchDefault)
    # &&& Need to do the math for all the
    
    # Make the offset joints and setup all the parenting of twists (last joint can't logically have twists)
    subArmature = []
    rotationOffsetCtrls = []
    for i, j in enumerate(mainArmature[:-1]):
        offset = duplicate(j, po=True)[0]
        offset.setParent(j)
        offset.rename( simpleName(j, '{}_Twist') )
        subArmature.append(offset)
        rotationOffsetCtrls.append(offset)
        
        attrName = simpleName(j, '{}_Twist')
        ctrl.addAttr( attrName, at='double', k=True )
        ctrl.attr(attrName) >> offset.rx
        
        if i in twists:
            for subTwist in subTwists[j]:
                subTwist.setParent(j)
                subArmature.append(subTwist)
                
                attrName = simpleName(subTwist)
                ctrl.addAttr( attrName, at='double', k=True )
                ctrl.attr(attrName) >> subTwist.rx
                
                #offset.rename( simpleName(j, '{0}_0ffset') )
                

    #for mainJoint, (startSegment, endSegment) in zip( mainArmature, zip( rotationOffsetCtrls, rotationOffsetCtrls[1:] + [mainArmature[-1]] )):
    #    if mainJoint in subTwists:
    #        twistSetup(subTwists[mainJoint], startSegment, endSegment)
    
    # Since we don't want twists affecting eachother, base them off the mainArmature
    for startSegment, endSegment in zip( mainArmature, mainArmature[1:] ):
        #print( 'HAS SUB TWISTS', startSegment in subTwists )
        if startSegment in subTwists:
            twistSetup(ctrl, subTwists[startSegment], startSegment, endSegment)
            
            
    '''
    # Build the groups to hold the twist controls
    groups = []
    for i, (j, nextJ) in enumerate(zip(mainArmature[:-1], mainArmature[1:])):
        g = group(em=True)
        parentConstraint(j, g)
        g.rename( core.dagObj.simpleName(g, '{0}_grp') )
        groups.append(g)

        g.setParent(container)
        
        if j in subTwists:
            
            #totalDist = core.dagObj.distanceBetween(j, nextJ)
            
            for subTwist in subTwists[j]:
                
                dist = core.dagObj.distanceBetween(j, subTwist)
                
                #disc = controller.control.disc()
                disc = controller.control.build('Twist', {'shape': controller.control.disc, 'align': 'x', 'size': 3})
                disc.setParent(g)
                disc.t.set( 0, 0, 0 )
                disc.r.set( 0, 0, 0 )
                
                core.dagObj.lockAll(disc)
                disc.rx.unlock()
                disc.tx.unlock()
                
                # Manage the lengths of the twist joints and their controls
                mult = core.math.multiply( dist, jointLenMultiplier)
                mult >> disc.tx
                mult >> subTwist.tx
                
                disc.rx >> subTwist.rx
    '''

    constraints = constrainAtoB( chain, subArmature + [mainArmature[-1]] )
    
        
    # PoleVector
    if not pvLen or pvLen < 0:
        pvLen = chainLength(mainArmature) * 0.5
    out = calcOutVector(mainArmature[0], mainArmature[1], mainArmature[-1])
    pvPos = out * pvLen + dt.Vector(xform(mainArmature[1], q=True, ws=True, t=True))
    pvCtrl = controller.control.build( name + '_pv', controlSpec['pv'], type=controller.control.POLEVECTOR )
    
    lockScale(lockRot(pvCtrl))
    xform(pvCtrl, ws=True, t=pvPos)
    controller.control.line(pvCtrl, mainArmature[1] )
    poleVectorConstraint( pvCtrl, mainIk )
    core.dagObj.zero(pvCtrl).setParent(container)
    
    # Socket offset control
    socketOffset = controller.control.build( name + '_socket', controlSpec['socket'], type=controller.control.TRANSLATE )
    socketContainer = parentGroup( start )
    socketContainer.setParent( container )
    
    core.dagObj.moveTo( socketOffset, start )
    core.dagObj.zero( socketOffset ).setParent( socketContainer )
    lockRot( socketOffset )
    lockScale( socketOffset )
    pointConstraint( socketOffset, mainArmature[0] )
    
    # Reuse the socketOffset container for the controlling chain
    mainArmature[0].setParent( socketContainer )
#    hide( mainArmature[0] )
    
    ''' Currently unable to get this to update, maybe order of operations needs to be enforced?
    # Add switch to reverse the direction of the bend
    reverseAngle = controlChain[1].jointOrient.get()[1] * -1.1
    ctrl.addAttr( 'reverse', at='short', min=0, max=1, dv=0, k=True )
    preferredAngle = core.math.condition( ctrl.reverse, '=', 0, 0, reverseAngle )
    twist = core.math.condition( ctrl.reverse, '=', 0, 0, -180)
    preferredAngle >> controlChain[1].preferredAngleY
    twist >> mainIk.twist
    core.math.condition( mainIk.twist, '!=', 0, 0, 1 ) >> mainIk.twistType # Force updating??
    '''
    
    # Register all the parts of the control for easy identification at other times.
    ctrl = nodeApi.RigController.convert(ctrl)
    ctrl.container = container
    ctrl.subControl['pv'] = pvCtrl
    ctrl.subControl['socket'] = socketOffset

    # Add default spaces
    space.addWorld( pvCtrl )
    #space.add( pvCtrl, ctrl, spaceName=shortName(ctrl, '{0}_posOnly') )
    #space.add( pvCtrl, ctrl, spaceName=shortName(ctrl, '{0}_posOnly'), mode=space.TRANSLATE)
    space.add( pvCtrl, ctrl )
    space.add( pvCtrl, ctrl, mode=space.Mode.TRANSLATE)
    
    return ctrl, constraints


@adds('stretch', 'bend', 'length')
@defaultspec( {'shape': controller.control.box,    'size': 10, 'color': 'green 0.22' },
           pv={'shape': controller.control.sphere, 'size': 5,  'color': 'green 0.22' },
       socket={'shape': controller.control.sphere, 'size': 5,  'color': 'green 0.22', 'visGroup': 'socket' } )
def dogleg(hipJoint, end, pvLen=None, name='Dogleg', endOrientType=EndOrient.TRUE_ZERO_FOOT, groupName='', controlSpec={}):
    '''
    ..  todo::
        * Specify toe joint instead to remove ambiguity in case of twist joints.
        * For some reason, sometimes, twist must be introduced because some flippin
            occurs.  For some reason the poleVector doesn't come in straight on.
            * Need to determine if a 180 twist is needed as the minotaur did.
        * Need to figure out the best way to constrain the last joint to the controller
    '''

    boundChain = getChain(hipJoint, end)

    container = group(n=name + '_dogHindleg', em=True, p=lib.getNodes.mainGroup())
    
    # &&& I think I want to turn this into the container for all extra stuff related to a given control
    chainGrp = group( p=container, n=name + "_ikChain", em=True )
    parentConstraint( hipJoint.getParent(), chainGrp, mo=True )
    
    # Make the control to translate/offset the limb's socket.
    socketOffset = controller.control.build( name + '_socket', controlSpec['socket'], type=controller.control.TRANSLATE )
    lockScale(socketOffset)
    lockRot(socketOffset)
    core.dagObj.moveTo( socketOffset, hipJoint )
    socketZero = core.dagObj.zero(socketOffset)
    socketZero.setParent( chainGrp )
    
    footCtrl = controller.control.build( name, controlSpec['main'], type=controller.control.IK)
    lockScale(footCtrl)
    footCtrl.addAttr( 'bend', at='double', k=True )
    core.dagObj.moveTo( footCtrl, end )
    
    if endOrientType == EndOrient.TRUE_ZERO:
        trueZeroSetup(end, footCtrl)
    elif endOrientType == EndOrient.TRUE_ZERO_FOOT:
        trueZeroFloorPlane(end, footCtrl)
    elif endOrientType == EndOrient.JOINT:
        core.dagObj.matchTo(footCtrl, end)
        
        footCtrl.rx.set( shortestAxis(footCtrl.rx.get()) )
        footCtrl.ry.set( shortestAxis(footCtrl.ry.get()) )
        footCtrl.rz.set( shortestAxis(footCtrl.rz.get()) )
        
        core.dagObj.zero(footCtrl)
    elif endOrientType == EndOrient.WORLD:
        # Do nothing, it's built world oriented
        pass
    
    createMatcher(footCtrl, end).setParent(container)

    # Make the main ik chain which gives overall compression
    masterChain = dupChain(hipJoint, end)
    masterChain[0].rename( simpleName(hipJoint, '{0}_OverallCompression') )

    mainIk = ikHandle( sol='ikRPsolver', sj=masterChain[0], ee=masterChain[-1] )[0]
    PyNode('ikSpringSolver').message >> mainIk.ikSolver
    
    mainIk.rename('mainIk')
    hide(mainIk)
    
    springFixup = group(em=True, n='SprinkIkFix')
    springFixup.inheritsTransform.set(False)
    springFixup.inheritsTransform.lock()
    springFixup.setParent( socketOffset )
    pointConstraint( socketOffset, springFixup )
    masterChain[0].setParent( springFixup )
    
    #pointConstraint( socketOffset, hipJoint )
    
    # Create the polevector.  This needs to happen first so things don't flip out later
    out = calcOutVector(masterChain[0], masterChain[1], masterChain[-1])
    if not pvLen or pvLen < 0:
        pvLen = chainLength(masterChain[1:]) * 0.5
    pvPos = out * pvLen + dt.Vector(xform(boundChain[1], q=True, ws=True, t=True))
    
    pvCtrl = controller.control.build( name + '_pv', controlSpec['pv'], type=controller.control.POLEVECTOR )
    lockScale(pvCtrl)
    lockRot(pvCtrl)
    xform(pvCtrl, ws=True, t=pvPos)
    poleVectorConstraint( pvCtrl, mainIk )
    
    # Verify the knees are in the same place
    delta = boundChain[1].getTranslation('world') - masterChain[1].getTranslation('world')
    if delta.length() > 0.1:
        mainIk.twist.set(180)
    
    # Make sub IKs so the chain can be offset
    offsetChain = dupChain(hipJoint, end)
    hide(offsetChain[0])
    offsetChain[0].rename( 'OffsetChain' )
    offsetChain[0].setParent(container)
    controller.control.line(pvCtrl, offsetChain[1] )
    constraints = constrainAtoB( getChain(hipJoint, end), offsetChain, mo=False )
    
    pointConstraint( masterChain[0], offsetChain[0] )
    ankleIk = ikHandle( sol='ikRPsolver', sj=offsetChain[0], ee=offsetChain[-2])[0]
    offsetIk = ikHandle( sol='ikRPsolver', sj=offsetChain[-2], ee=offsetChain[-1])[0]
    offsetIk.rename('metatarsusIk')
    
    offsetControl = group(em=True, n='OffsetBend')
    offsetContainer = group(offsetControl, n='OffsetSpace')
    offsetContainer.setParent(footCtrl)
        
    # Setup the offsetContainer so it is properly aligned to bend on z
    offsetContainer.setParent( masterChain[-1] )
    offsetContainer.t.set(0, 0, 0)
    #temp = aimConstraint( pvCtrl, offsetContainer, aim=[1, 0, 0], wut='object', wuo=hipJoint, u=[0, 1, 0])
    #delete( temp )
    
    '''
    NEED TO CHANGE THE ORIENTATION
    
    Must perfectly align with ankle segment so the offset ikhandle can translate
    according to how much things are scaled
    
    '''
    lib.anim.orientJoint(offsetContainer, boundChain[-2], upTarget=boundChain[-3], aim='y', up='x')
    #mimic old way lib.anim.orientJoint(offsetContainer, pvCtrl, upTarget=hipJoint, aim='x', up='y')
    #lib.anim.orientJoint(offsetContainer, pvCtrl, upTarget=hipJoint, aim='x', up='y')
    
    
    offsetControl.t.set(0, 0, 0)
    offsetControl.t.lock()
    offsetControl.r.set(0, 0, 0)
    footCtrl.bend >> offsetControl.rz
    
    '''
    This is really dumb.
    Sometimes maya will rotate everything by 180 but I'm not sure how to
    calculate the proper offset, which normally results in one axis being off
    by 360, so account for that too.
    '''
    temp = orientConstraint( footCtrl, offsetChain[-1], mo=True)
    
    if not core.math.isClose( offsetChain[-1].r.get(), [0, 0, 0] ):

        badVals = offsetChain[-1].r.get()
        delete(temp)
        offsetChain[-1].r.set( -badVals )
        temp = orientConstraint( footCtrl, offsetChain[-1], mo=True)

        for a in 'xyz':
            val = offsetChain[-1].attr('r' + a).get()
            if abs(val - 360) < 0.00001:
                attr = temp.attr( 'offset' + a.upper() )
                attr.set( attr.get() - 360 )
                
            elif abs(val + 360) < 0.00001:
                attr = temp.attr( 'offset' + a.upper() )
                attr.set( attr.get() + 360 )
    # Hopefully the end of dumbness


    
    ankleIk.setParent( offsetControl )
    
    # Adjust the offset ikHandle according to how long the final bone is.

    if masterChain[-1].tx.get() > 0:
        masterChain[-1].tx >> ankleIk.ty
    else:
        core.math.multiply(masterChain[-1].tx, -1.0) >> ankleIk.ty
    
    ankleIk.tx.lock()
    ankleIk.tz.lock()
    
    #ankleIk.t.lock()
    
    
    
    
    
    mainIk.setParent( footCtrl )
    offsetIk.setParent( footCtrl )
    
    core.dagObj.zero(footCtrl).setParent( container )
    
    hide(masterChain[0], ankleIk, offsetIk)
    poleVectorConstraint( pvCtrl, ankleIk )
    poleVectorConstraint( pvCtrl, offsetIk )
    
    # Adding the pv constraint might require a counter rotation of the offsetIk
    counterTwist = offsetChain[-2].rx.get() * (1.0 if offsetChain[-2].tx.get() < 0 else -1.0)
    offsetIk.twist.set( counterTwist )
    
    core.dagObj.zero(pvCtrl).setParent( container )
    
    # Make stretchy ik, but the secondary chain needs the stretch hooked up too.
    makeStretchyNonSpline(footCtrl, mainIk)
    #for src, dest in zip( getChain(masterChain, masterEnd)[1:], getChain( hipJoint, getDepth(hipJoint, 4) )[1:] ):
        #src.tx >> dest.tx
        
    for src, dest in zip( masterChain[1:], offsetChain[1:] ):
        src.tx >> dest.tx
    
    footCtrl = nodeApi.RigController.convert(footCtrl)
    footCtrl.container = container
    footCtrl.subControl['pv'] = pvCtrl
    footCtrl.subControl['socket'] = socketOffset
    
    # Add default spaces
    space.addWorld( pvCtrl )
    space.add( pvCtrl, footCtrl )
    space.add( pvCtrl, footCtrl, mode=space.Mode.TRANSLATE)
    if hipJoint.getParent():
        space.add( pvCtrl, hipJoint.getParent())
    
        space.addWorld( footCtrl )
        space.add( footCtrl, hipJoint.getParent() )
    
    return footCtrl, constraints


class TwistStyle:
    '''
    Used by splineIk.  Advanced uses advanced twist while the others determin
    which rotation axis drives the twist attribute.
    '''
    ADVANCED = 'Advanced'
    X        = 'X'
    NEG_X    = '-X'
    Y        = 'Y'
    NEG_Y    = '-Y'
    Z        = 'Z'
    NEG_Z    = '-Z'
    
    @classmethod
    def asChoices(cls):
        choices = collections.OrderedDict()
        choices[cls.ADVANCED]   = cls.ADVANCED
        choices[cls.X]          = cls.X
        choices[cls.NEG_X]      = cls.NEG_X
        choices[cls.Y]          = cls.Y
        choices[cls.NEG_Y]      = cls.NEG_Y
        choices[cls.Z]          = cls.Z
        choices[cls.NEG_Z]      = cls.NEG_Z
        return choices


@adds('twist', 'stretch')
@defaultspec( {'shape': controller.control.sphere, 'size': 10, 'color': 'blue 0.22'} )
def splineIk(start, end, controlCountOrCrv=4, twistInfDist=0, simplifyCurve=False,
    tipBend=True, sourceBend=True, matchOrient=True, allowOffset=False,  # noqa e128
    useLeadOrient=False,  # This is an backwards compatible option, mutually exclusive with matchOrient
    twistStyle=TwistStyle.ADVANCED, duplicateCurve=True,
    name='', groupName='', controlSpec={}):
    '''
    Make a spline controller from `start` to `end`.
    
    :param int twistInfDist: Default twist controls to falloff before hitting eachother.
        Otherwise it is the number of joints on either side it will influence.
    :param bool simplifyCurve:  Only used if # of cvs is specified.  Turning it
        on will likely result it the curve not matching the existing joint position
        but will be more evenly spaced per control.
    :param bool tipBend:  If True, an extra cv will be added at the second to
        last joint, controlled by the last controller to ease out.
        
    ##:param bool applyDirectly: If True, rig the given joints, do not make a duplicate chain
        
    :param bool useLeadOrient: If True, the controllers will be aligned the same
        as the first joint.
        **NOTE** I think this option only exists to preserve previous builds, this is pretty dumb
        
    :param bool matchOrient: Does trueZero on the start and end.  I'm not sure this makes sense.
        
    
    
    ..  todo::
        * Add the same spline chain +X towards child that the neck has and test out advancedTwist()
    
        * See if I can identify the closest joint to a control and orient to that
        * The first joint has parent AND local, which are the same thing, keep this for convenience of selecting all the controls and editing attrs?
        * Test specifying your own curve
        * There is a float division error that can happen if there are too many control cvs.
        * Verify twists work right with unsimplified curves (hint, I don't think they do).
    '''
    
    if isinstance( controlCountOrCrv, int ):
        assert controlCountOrCrv > 3, "controlCount must be at least 4"
    
    # The axis to twist and stretch on.
    jointAxis = identifyAxis( start.listRelatives(type='joint')[0] )
    
    # Make a duplicate chain for the IK that will also stretch.
    stretchingChain = dupChain( start, end, '{0}_stretch' )
    
    # &&& NOTE!  This might affect advanced twist in some way.
    # If the chain is mirrored, we need to reorient to point down x so the
    # spline doesn't mess up when the main control rotates
    if stretchingChain[1].tx.get() < 0:
        # Despite aggresive zeroing of the source, the dup can still end up slightly
        # off zero so force it.
        for jnt in stretchingChain:
            jnt.r.set(0, 0, 0)
        joint( stretchingChain[0], e=True, oj='xyz', secondaryAxisOrient='yup', zso=True, ch=True)
        joint( stretchingChain[-1], e=True, oj='none')
    
    if isinstance( controlCountOrCrv, int ):
        mainIk, _effector, crv = ikHandle( sol='ikSplineSolver',
            sj=stretchingChain[0],
            ee=stretchingChain[-1],
            ns=controlCountOrCrv - 3,
            simplifyCurve=simplifyCurve)
    else:
        if duplicateCurve:
            crv = duplicate(controlCountOrCrv)[0]
        else:
            crv = controlCountOrCrv
            
        mainIk, _effector = ikHandle( sol='ikSplineSolver',
            sj=stretchingChain[0],
            ee=stretchingChain[-1],
            ccv=False,
            pcv=False)
        crv.getShape().worldSpace[0] >> mainIk.inCurve
    
    hide(mainIk)
    mainIk.rename( simpleName(start, "{0}_ikHandle") )
    crv.rename( simpleName(start, "{0}_curve") )
        
    if not name:
        name = trimName(start)

    if name.count(' '):
        name, endName = name.split()
    else:
        endName = ''
    
    # Only add a tipBend cv if number of cvs was specified.
    if tipBend and isinstance( controlCountOrCrv, int ):
        currentTrans = [ xform(cv, q=True, ws=True, t=True) for cv in crv.cv ]
        insertKnotCurve( crv.u[1], nk=1, add=False, ib=False, rpo=True, cos=True, ch=True)
        for pos, cv in zip(currentTrans, crv.cv[:-2]):
            xform( cv, ws=True, t=pos )
    
        xform( crv.cv[-2], ws=True, t=xform(end.getParent(), q=True, ws=True, t=True) )
        xform( crv.cv[-1], ws=True, t=currentTrans[-1] )
        
    # Only add a sourceBend cv if number of cvs was specified.
    if sourceBend and isinstance( controlCountOrCrv, int ):
        currentTrans = [ xform(cv, q=True, ws=True, t=True) for cv in crv.cv ]
        insertKnotCurve( crv.u[1.2], nk=1, add=False, ib=False, rpo=True, cos=True, ch=True)  # I honestly don't know why, but 1.2 must be different than 1.0
        for pos, cv in zip(currentTrans[1:], crv.cv[2:]):
            xform( cv, ws=True, t=pos )
    
        xform( crv.cv[0], ws=True, t=currentTrans[0] )
        xform( crv.cv[1], ws=True, t=xform(stretchingChain[1], q=True, ws=True, t=True) )
    
    grp = group(em=True, p=lib.getNodes.mainGroup(), n=start.name() + "_splineTwist")
    
    controls = addControlsToCurve(name + 'Ctrl', crv, controlSpec['main'])
    for ctrl in controls:
        core.dagObj.zero(ctrl).setParent( grp )

    if endName:
        controls[-1].rename(endName + 'Ctrl')

    if matchOrient:
        trueZeroSetup(start, controls[0])
        trueZeroSetup(end, controls[-1])

    if tipBend:
        if useLeadOrient and not matchOrient:
            controls[-1].setRotation( end.getRotation(space='world'), space='world' )
        
        parent( controls[-2].getChildren(), controls[-1] )
        name = controls[-2].name()
        delete( core.dagObj.zero(controls[-2]) )

        if not endName:
            controls[-1].rename(name)
        controls[-2] = controls[-1]
        controls.pop()
        #core.dagObj.zero(controls[-2]).setParent(controls[-1])
        #channels = [t + a for t in 'trs' for a in 'xyz']
        #for channel in channels:
            #controls[-2].attr( channel ).setKeyable(False)
            #controls[-2].attr( channel ).lock()
           
    if sourceBend:
        names = []
        
        for ctrl in controls[1:-1]:
            names.append( ctrl.name() )
            ctrl.rename( '__temp' )
        
        endNum = -1 if endName else None
        for name, cur in zip(names, controls[2:endNum] ):
            cur.rename(name)
            
        if useLeadOrient and not matchOrient:
            controls[0].setRotation( start.getRotation(space='world'), space='world' )
            
        parent( controls[1].getChildren(), controls[0] )
        delete( core.dagObj.zero(controls[1]) )
        
        del controls[1]
        
    controls[0] = nodeApi.RigController.convert(controls[0])
    controls[0].container = grp
    
    stretchAttr, jointLenMultiplier = makeStretchySpline(controls[0], mainIk)
        
    connectingCurve = addConnectingCurve(controls)
    controls[0].visibility >> connectingCurve.visibility
    
    # Make twist for everything but hide them all and drive the ones that overlap
    # with spline controllers by the spline control.
    if not twistInfDist:
        numJoints = countJoints(start, end)
        twistInfDist = int(math.ceil( numJoints - len(controls) ) / float(len(controls) - 1))
        twistInfDist = max(1, twistInfDist)
    
    noInherit = group(em=True, p=grp, n='NoInheritTransform')
    lockTrans(noInherit)
    lockRot(noInherit)
    lockScale(noInherit)
    noInherit.inheritsTransform.set(False)
    noInherit.inheritsTransform.lock()

    # &&& If simplify curve is ON, the last joint gets constrained to the spinner?
    # Otherwise it gets constrained to the offset or stretch joint, which I think is correct.
    
    if allowOffset:
        # If allowOffset, make another chain to handle the difference in joint positions.
        offsetChain = dupChain( start, end, '{0}_offset' )

        offsetChain[0].setParent(noInherit)
        hide(offsetChain[0])
        twists, constraints = addTwistControls( offsetChain, start, end, twistInfDist)
        finalRigJoint = offsetChain[-1]
    else:
        twists, constraints = addTwistControls( stretchingChain, start, end, twistInfDist )
        finalRigJoint = stretchingChain[-1]
    
    # Constrain the end to the last controller so it doesn't pop off at all,
    # but still respect the stretch attr.
    pointConstraint(finalRigJoint, end, e=True, rm=True)
    
    # Make a proxy that can allows respecting stretch being active or not.
    endProxy = duplicate(end, po=True)[0]
    endProxy.rename('endProxy')
    hide(endProxy)
    endProxy.setParent(grp)
    
    stretchAttr >> core.constraints.pointConst( controls[-1], endProxy, mo=True )
    core.math.opposite(stretchAttr) >> core.constraints.pointConst( finalRigJoint, endProxy )
    constraints.point >> core.constraints.pointConst( endProxy, end )
    
    hide(twists)
    indicies = []
    numControls = len(controls)
    numTwists = len(twists)
    for i, ctrl in enumerate(controls):
        index = int(round( i * ((numTwists - 1) / (numControls - 1)) ))
        drive( ctrl, 'twist', twists[index].attr('r' + jointAxis) )
        space.add( ctrl, start.getParent(), 'local' )
    
    parents = [start.getParent()] + controls[:-1]
    
    stretchingChain[0].setParent(noInherit)
    crv.setParent(noInherit)
    hide(crv, stretchingChain[0])
    connectingCurve.setParent( noInherit )
    
    mainIk.setParent(grp)
    
    # Do not want to scale but let rotate for "fk-like" space mode
    for ctrl, _parent in zip(controls, parents):
        lockScale( ctrl )
        
        if useLeadOrient:
            ctrl.setRotation( start.getRotation(space='world'), space='world' )
            core.dagObj.zero(ctrl)
        
        space.addWorld(ctrl)
        space.add( ctrl, _parent, 'parent')
    
    for i, ctrl in enumerate(controls[1:]):
        controls[0].subControl[str(i)] = ctrl
    
    # Must constrain AFTER controls (possibly) get orientd
    orientConstraint( controls[-1], finalRigJoint, mo=True )

    # Setup advanced twist
    if twistStyle == TwistStyle.ADVANCED:
        # &&& Test using advancedTwist() to replace the code beloew
        advancedTwist(stretchingChain[0], stretchingChain[1], controls[0], controls[-1], mainIk)
        '''
        startAxis = duplicate( start, po=True )[0]
        startAxis.rename( 'startAxis' )
        startAxis.setParent( controls[0] )
        
        endAxis = duplicate( start, po=True )[0]
        endAxis.rename( 'endAxis' )
        endAxis.setParent( controls[-1] )
        endAxis.t.set(0, 0, 0)
        
        mainIk.dTwistControlEnable.set(1)
        mainIk.dWorldUpType.set(4)
        startAxis.worldMatrix[0] >> mainIk.dWorldUpMatrix
        endAxis.worldMatrix[0] >> mainIk.dWorldUpMatrixEnd
        
        hide(startAxis, endAxis)
        '''
    else:
        if twistStyle == TwistStyle.X:
            controls[-1].rx >> mainIk.twist
        elif twistStyle == TwistStyle.NEG_X:
            core.math.multiply(controls[-1].rx, -1.0) >> mainIk.twist
            
        elif twistStyle == TwistStyle.Y:
            controls[-1].ry >> mainIk.twist
        elif twistStyle == TwistStyle.NEG_Y:
            core.math.multiply(controls[-1].ry, -1.0) >> mainIk.twist
            
        elif twistStyle == TwistStyle.Z:
            controls[-1].rz >> mainIk.twist
        elif twistStyle == TwistStyle.NEG_Z:
            core.math.multiply(controls[-1].rz, -1.0) >> mainIk.twist
        
        # To make .twist work, the chain needs to follow parent joint
        follow = group(em=True, p=grp)
        target = start.getParent()
        core.dagObj.matchTo(follow, stretchingChain[0])
        parentConstraint( target, follow, mo=1 )
        follow.rename(target + '_follow')
        stretchingChain[0].setParent(follow)
        
    # Constraint the offset (if exists) to the stretch last to account for any adjustments.
    if allowOffset:
        constrainAtoB(offsetChain[:-1], stretchingChain[:-1])
        pointConstraint(stretchingChain[-1], offsetChain[-1], mo=True)

    return controls[0], constraints


@adds()
@defaultspec( {'shape': controller.control.box,    'size': 10, 'color': 'blue  0.22'},
       manual={'shape': controller.control.sphere, 'size':  5, 'color': 'green 0.22'}
 )
def squashAndStretch(joints, squashCenter, orientAsParent=True, rangeMin=-5, rangeMax=5, scaleMin=0.5, scaleMax=2, controlSpec={}):
    '''
    :param joints: List of joints that will scale
    :param squashCenter: The worldspace center point to place the master squash control.
    :param orientAsParent: Weather the control should be oriented ?? Does this make sense?... Probably not
    '''
    
    squashCenter = dt.Vector(squashCenter)
    container = parentGroup(joints[0])
    container.setParent( lib.getNodes.mainGroup() )
    
    mainCtrl = controller.control.build(   trimName(joints[0].getParent()) + "SquashMain_ctrl",
                                controlSpec['main'],
                                type=controller.control.TRANSLATE )
    mainCtrl = nodeApi.RigController.convert(mainCtrl)
    mainCtrl.setParent(container)
    
    mainCtrl.addAttr( 'size', at='double', min=rangeMin, max=rangeMax, dv=0.0, k=True )
    
    lockScale(mainCtrl)
    
    if orientAsParent:
        core.dagObj.matchTo( mainCtrl, joints[0].getParent() )
                            
    xform(mainCtrl, ws=True, t=squashCenter)
    
    core.dagObj.zero(mainCtrl)
    
    subControls = []
    for i, j in enumerate(joints):
        subCtrl = controller.control.build(trimName(j) + "_ctrl",
                                controlSpec['manual'],
                                type=controller.control.TRANSLATE )
        subControls.append(subCtrl)
        core.dagObj.matchTo(subCtrl, j)
        subCtrl.setParent(container)
        core.dagObj.zero(subCtrl)
        lockRot(subCtrl)
        lockScale(subCtrl)
        
        scalingLoc = spaceLocator()
        scalingLoc.rename( trimName(j) + '_squasher' )
        core.dagObj.matchTo(scalingLoc, j)
        hide(scalingLoc)
        scalingLoc.setParent(mainCtrl)
        
        space.add(subCtrl, scalingLoc)
                
        ctrlPos = dt.Vector(xform(subCtrl, q=True, ws=True, t=True))
        
        setDrivenKeyframe( scalingLoc, at=['tx', 'ty', 'tz'], cd=mainCtrl.size )
        
        mainCtrl.size.set(rangeMin)
        lower = (ctrlPos - squashCenter) * scaleMin + squashCenter
        xform(scalingLoc, ws=True, t=lower)
        setDrivenKeyframe( scalingLoc, at=['tx', 'ty', 'tz'], cd=mainCtrl.size )
        
        mainCtrl.size.set(rangeMax)
        upper = (ctrlPos - squashCenter) * scaleMax + squashCenter
        xform(scalingLoc, ws=True, t=upper)
        setDrivenKeyframe( scalingLoc, at=['tx', 'ty', 'tz'], cd=mainCtrl.size )
        
        mainCtrl.size.set(0.0)
        xform(scalingLoc, ws=True, t=(ctrlPos))
        
        mainCtrl.subControl[str(i)] = subCtrl
        
    constraints = constrainAtoB(joints, subControls)
    
    mainCtrl.container = container
    
    return mainCtrl, constraints


@adds()
@defaultspec( {'shape': controller.control.box,    'size': 10, 'color': 'blue 0.22'} )
def chainedIk(start, end, driveChain, handleInfo, splineOptions={}, controlSpec={}):
    '''
    driveChain will get the spline control to drive the start->end chain.
    
    The start->end chain will get a series daisy chained ik handles that are
    parented into the driveChain.
    
    ..  todo::
        Since I make the spline first, I think I might be able to disable the
        allowOffset as it won't make any difference (I think)
    
    '''
    
    chain = getChain(start, end)
    controlChain = dupChain(start, end)
    constraints = constrainAtoB( chain, controlChain )
    
    #container = parentGroup(joints[0])
    #container.setParent( lib.getNodes.mainGroup() )
    
    chunkStartIndex = 0
    
    if 'controlSpec' in splineOptions:
        del splineOptions['controlSpec']
        
    mainCtrl, _constraints = splineIk(driveChain[0], driveChain[-1], controlSpec=controlSpec, **splineOptions)
    
    for ikJoint, ikParent, pvParent in handleInfo:

        jIndex = chain.index(ikJoint.real)
        chunk = controlChain[chunkStartIndex:jIndex + 1]
        chunkStartIndex = jIndex

        out = calcOutVectorRaw(chunk[0], chunk[1], chunk[-1])
        
        ik = ikHandle( sol='ikRPsolver', sj=chunk[0], ee=chunk[-1] )[0]
        ik.rename( 'ik_' + ikJoint.name() )
        
        # PoleVector
        pvPos = out * chainLength(chunk) / 2.0 + dt.Vector(xform(chunk[1], q=True, ws=True, t=True))
        pv = spaceLocator(n='pv_' + ikJoint.name())
        pv.t.set(pvPos)
        poleVectorConstraint( pv, ik )
        
        pv.setParent(pvParent)
        ik.setParent(ikParent)
        
    hide(driveChain[0], controlChain[0])
    
    driveChain[0].setParent( mainCtrl.container )
    controlChain[0].setParent( mainCtrl.container )
    #mainCtrl = RigController.convert(mainCtrl)
    #mainCtrl.container = container
    
    parentConstraint( mainCtrl, controlChain[0], mo=True)
    
    return mainCtrl, constraints


@adds()
@defaultspec(      {'shape': controller.control.box,       'size': 4, 'color': 'blue 0.22'},
        toeControl={'shape': controller.control.box,       'size': 3, 'color': 'blue 0.22'},
         ballPivot={'shape': controller.control.sphere,    'size': 3, 'color': 'green 0.22'},
            toeTap={'shape': controller.control.pin,       'size': 3, 'color': 'green 0.22'},
         heelRaise={'shape': controller.control.pin,       'size': 3, 'color': 'red 0.22'},
            )
def foot(ballJnt, toePos, heelPos, legControl, side, controlSpec={}):
    # The foot container
    container = group(n='Foot_Deal', em=True, p=lib.getNodes.mainGroup())
    
    if not side:
        side = ''
    
    # Fake joints for IK/FK switching tech
    ankle = joint(None, n='FakeAnkle')
    ball = joint(n='FakeBall')
    toe = joint(n='FakeToe')
    
    # IK gathering
    ballIk, effector = ikHandle(solver='ikSCsolver', sj=ankle, ee=ball)
    toeIk, effector = ikHandle(solver='ikSCsolver', sj=ball, ee=toe)
    
    # Place the "Fake" joints
    core.dagObj.moveTo(ankle, legControl)
    core.dagObj.moveTo(ball, ballJnt)
    core.dagObj.moveTo(toe, toePos)
    
    #Foot Control
    footCtrl = controller.control.build( "Foot_" + side + "_ctrl", controlSpec['main'], type=controller.control.TRANSLATE )
    core.dagObj.moveTo(footCtrl, heelPos)
    footCtrl.setParent(container)
    core.dagObj.zero(footCtrl)
        
    # Toe Control
    toeCtrl = controller.control.build( "Toe_" + side + "_ctrl", controlSpec['toeControl'], type=controller.control.TRANSLATE )
    core.dagObj.matchTo(toeCtrl, toe)
    toeCtrl.setRotation( legControl.getRotation(space='world'), space='world' )
    toeCtrl.setParent(footCtrl)
    ankle.setParent(toeCtrl)
    core.dagObj.zero(toeCtrl)
    
    # BaLL Control
    ballCtrl = controller.control.build( "Ball_" + side + "_ctrl", controlSpec['ballPivot'], type=controller.control.TRANSLATE )
    core.dagObj.moveTo(ballCtrl, ballJnt)
    ballIk.setParent(ballCtrl)
    ballCtrl.setParent(toeCtrl)
    core.dagObj.zero(ballCtrl)
    
    # Toe Tap Control
    toeTapCtrl = controller.control.build( "ToeTap_" + side + "_ctrl", controlSpec['toeTap'], type=controller.control.ROTATE )
    core.dagObj.moveTo(toeTapCtrl, toePos)
    toeIk.setParent(toeTapCtrl)
    toeTapCtrl.setParent(ballCtrl)
    core.dagObj.zero(toeTapCtrl)
    xform(toeTapCtrl, ws=True, rp=xform(ballJnt, q=True, ws=True, t=True))
    
    # Heel Raise Control
    heelRaiseCtrl = controller.control.build( "HeelRaise_" + side + "_ctrl", controlSpec['heelRaise'], type=controller.control.ROTATE )
    core.dagObj.moveTo(heelRaiseCtrl, heelPos)
    xform(heelRaiseCtrl, ws=True, rp=xform(ballJnt, q=True, ws=True, t=True))
    
    select(d=True)
    space.add(legControl, heelRaiseCtrl)
    heelRaiseCtrl.setParent(ballCtrl)
    core.dagObj.zero(heelRaiseCtrl)
    
    # Set the leg control Pivot to the heel
    xform(legControl, ws=True, rp=heelPos)
    
    # Set up the Ctrl object
    ctrl = nodeApi.RigController.convert(toeCtrl)
    ctrl.container = container
    ctrl.subControl['ballPivot'] = ballCtrl
    ctrl.subControl['toeTap'] = toeTapCtrl
    ctrl.subControl['heelRaise'] = heelRaiseCtrl
    ctrl.subControl['toeControl'] = toeCtrl
    
    constraints = constrainAtoB( [ballJnt], [ball], mo=True )
    #constraints = []
    
    return ctrl, constraints
